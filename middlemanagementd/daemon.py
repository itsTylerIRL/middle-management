"""The core service: scheduler, control API, audio-client hub and interrogation lifecycle.

Wire protocol on /ws/audio (JSON text frames):
  client -> daemon  {"type": "hello", "name": str, "version": str}
  daemon -> client  {"type": "op", "id": int, "op": "chime"|"say"|"listen"|"hold_music", "args": {...}}
  daemon -> client  {"type": "cancel", "id": int}      abort one op
  daemon -> client  {"type": "stop"}                   kill switch: abort everything now
  client -> daemon  {"type": "result", "id": int, "ok": bool, "data": {...}, "error": str, "kind": str}
  client -> daemon  {"type": "hushed"}                 user silenced the client locally
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import itertools
import logging
import os
import random
import signal
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from aiohttp import WSMsgType, web

from . import __version__
from .audio.port import (AudioError, AudioPort, AudioSilenced, AudioUnavailable, BargeIn, ListenRequest,
                         ListenResult, SayResult)
from .config import Config, expand
from .escalation import LEVEL_NAMES
from .evaluator import JargonEvaluator
from .llm import LLMClient
from .logs import event
from .reputation import Store, rank_for
from .scheduler import Scheduler
from .session import InterrogationRunner

log = logging.getLogger(__name__)


@dataclass
class ClientConn:
    ws: web.WebSocketResponse
    name: str
    connected_at: float = field(default_factory=time.time)
    pending: dict[int, asyncio.Future[dict[str, Any]]] = field(default_factory=dict)


class AudioHub:
    """Tracks the connected audio client (latest connection wins) and does request/response over it."""

    def __init__(self) -> None:
        self.client: ClientConn | None = None
        self._ids = itertools.count(1)
        self.on_hushed: Any = None

    @property
    def connected(self) -> bool:
        return self.client is not None and not self.client.ws.closed

    async def request(self, op: str, args: dict[str, Any], timeout: float) -> dict[str, Any]:
        conn = self.client
        if conn is None or conn.ws.closed:
            raise AudioUnavailable("no audio client connected")
        rid = next(self._ids)
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        conn.pending[rid] = fut
        try:
            await conn.ws.send_json({"type": "op", "id": rid, "op": op, "args": args})
            return await asyncio.wait_for(fut, timeout)
        except TimeoutError:
            await self._cancel(conn, rid)
            raise AudioUnavailable(f"audio client timed out on {op}") from None
        except asyncio.CancelledError:
            # Session cancelled (kill switch, hold music ending): stop this op on the client too.
            await self._cancel(conn, rid)
            raise
        except ConnectionResetError as e:
            raise AudioUnavailable("audio client connection lost") from e
        finally:
            conn.pending.pop(rid, None)

    @staticmethod
    async def _cancel(conn: ClientConn, rid: int) -> None:
        with contextlib.suppress(Exception):
            await conn.ws.send_json({"type": "cancel", "id": rid})

    def handle_result(self, conn: ClientConn, msg: dict[str, Any]) -> None:
        fut = conn.pending.get(int(msg.get("id", -1)))
        if fut is None or fut.done():
            return
        if msg.get("ok"):
            fut.set_result(msg.get("data") or {})
        else:
            err = str(msg.get("error", "audio error"))
            exc = AudioSilenced(err) if msg.get("kind") == "silenced" else AudioUnavailable(err)
            fut.set_exception(exc)

    def drop(self, conn: ClientConn) -> None:
        for fut in conn.pending.values():
            if not fut.done():
                fut.set_exception(AudioUnavailable("audio client disconnected"))
        conn.pending.clear()
        if self.client is conn:
            self.client = None

    async def stop_all(self) -> None:
        if self.connected:
            assert self.client is not None
            with contextlib.suppress(Exception):
                await self.client.ws.send_json({"type": "stop"})


class RemoteAudioPort:
    def __init__(self, hub: AudioHub) -> None:
        self.hub = hub

    async def chime(self) -> None:
        await self.hub.request("chime", {}, timeout=20)

    async def say(self, text: str, *, length_scale: float | None = None, barge_in: BargeIn | None = None,
                  gain: float = 1.0) -> SayResult:
        args: dict[str, Any] = {"text": text, "length_scale": length_scale, "gain": gain}
        timeout = 40 + len(text) / 6
        if barge_in is not None:
            args["barge_in"] = barge_in.to_dict()
            timeout += barge_in.listen.max_seconds * 1.5 + 60  # may turn into a listen + transcription
        data = await self.hub.request("say", args, timeout=timeout)
        return SayResult(**{k: data[k] for k in SayResult.__dataclass_fields__ if k in data})

    async def listen(self, req: ListenRequest) -> ListenResult:
        timeout = req.start_timeout + req.max_seconds * 1.5 + 90
        data = await self.hub.request("listen", req.to_dict(), timeout=timeout)
        return ListenResult(**{k: data[k] for k in ("text", "speech_seconds", "interrupted", "no_speech") if k in data})

    async def hold_music(self, max_seconds: float) -> None:
        await self.hub.request("hold_music", {"max_seconds": max_seconds}, timeout=max_seconds + 15)

    async def stop(self) -> None:
        await self.hub.stop_all()


class Daemon:
    def __init__(self, cfg: Config, audio: AudioPort | None = None, rng: random.Random | None = None) -> None:
        self.cfg = cfg
        self.rng = rng or random.Random()
        self.store = Store(expand(cfg.storage.database_path))
        self.llm = LLMClient(cfg.llm)
        self.evaluator = JargonEvaluator(cfg.evaluation, self.llm, cfg.llm.max_tokens_eval, cfg.llm.eval_temperature)
        self.runner = InterrogationRunner(cfg, self.store, self.llm, self.evaluator, self.rng, emit=self._emit)
        self.scheduler = Scheduler(cfg.scheduler, self.rng)
        self.hub = AudioHub()
        self.local_audio = audio
        self.audio: AudioPort = audio or RemoteAudioPort(self.hub)
        self.events: deque[dict[str, Any]] = deque(maxlen=300)
        self._seq = itertools.count(1)
        self.session_task: asyncio.Task[Any] | None = None
        self.wake = asyncio.Event()
        self.shutdown_event = asyncio.Event()
        self.next_fire_at: datetime | None = None
        self.started_at = time.time()
        if cfg.scheduler.start_paused and self.store.get("paused") is None:
            self.store.set("paused", True)

    # --- state -------------------------------------------------------------
    @property
    def paused(self) -> bool:
        return bool(self.store.get("paused", False))

    @property
    def killed(self) -> bool:
        return bool(self.store.get("killswitch", False))

    @property
    def busy(self) -> bool:
        return self.session_task is not None and not self.session_task.done()

    @property
    def audio_ready(self) -> bool:
        return self.local_audio is not None or self.hub.connected

    def _emit(self, kind: str, data: dict[str, Any]) -> None:
        self.events.append({"seq": next(self._seq), "ts": time.time(), "type": kind, "data": data})

    def status(self) -> dict[str, Any]:
        _, rank = rank_for(self.store.points())
        level = self.store.level
        client = self.hub.client
        return {
            "version": __version__,
            "uptime_seconds": round(time.time() - self.started_at),
            "paused": self.paused,
            "killswitch": self.killed,
            "busy": self.busy,
            "scheduler_enabled": self.cfg.scheduler.enabled,
            "next_interrogation": self.next_fire_at.isoformat(timespec="seconds") if self.next_fire_at else None,
            "audio": ("local" if self.local_audio is not None else
                      {"name": client.name, "connected_at": client.connected_at} if self.hub.connected and client
                      else None),
            "escalation_level": level,
            "escalation_name": LEVEL_NAMES.get(level),
            "rank": rank,
            "llm_endpoint": self.cfg.llm.base_url if self.cfg.llm.enabled else None,
            "last_event_seq": self.events[-1]["seq"] if self.events else 0,
        }

    # --- control -----------------------------------------------------------
    def trigger(self, trigger: str = "manual", personality: str | None = None) -> tuple[bool, str]:
        if self.killed:
            return False, "kill switch engaged; run `middlemanagementd revive` first"
        if self.busy:
            return False, "an evaluation is already in progress"
        if not self.audio_ready:
            return False, "no audio client connected"
        self.session_task = asyncio.create_task(self._run_session(trigger, personality))
        return True, "interrogation started"

    async def _run_session(self, trigger: str, personality: str | None) -> None:
        try:
            await self.runner.run(self.audio, trigger=trigger, personality=personality)
        except asyncio.CancelledError:
            self._emit("session_aborted", {"reason": "killed"})
            with contextlib.suppress(Exception):
                await self.audio.stop()
            raise
        except AudioError as e:
            event(log, "interrogation aborted", logging.WARNING, error=str(e))
            self._emit("session_aborted", {"reason": str(e)})
        except Exception as e:  # noqa: BLE001 - one bad session must not kill the daemon
            log.exception("interrogation crashed")
            self._emit("session_aborted", {"reason": f"internal error: {e!r}"})
            with contextlib.suppress(Exception):
                await self.audio.stop()
        finally:
            self.wake.set()

    async def abort_session(self) -> None:
        task = self.session_task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(task, 5)

    async def kill(self) -> None:
        """Global kill switch: silence everything immediately and stay silent until revived."""
        self.store.set("killswitch", True)
        # Tell the client first so sound stops even if cancellation takes a moment.
        with contextlib.suppress(Exception):
            await self.audio.stop()
        await self.abort_session()
        self.next_fire_at = None
        self._emit("killswitch", {"engaged": True})
        event(log, "kill switch engaged")
        self.wake.set()

    def revive(self) -> None:
        self.store.set("killswitch", False)
        self._emit("killswitch", {"engaged": False})
        self.wake.set()

    def set_paused(self, paused: bool) -> None:
        self.store.set("paused", paused)
        self._emit("paused" if paused else "resumed", {})
        self.wake.set()

    # --- scheduler ---------------------------------------------------------
    async def scheduler_loop(self) -> None:
        while not self.shutdown_event.is_set():
            self.wake.clear()
            if self.killed or self.paused or not self.cfg.scheduler.enabled:
                self.next_fire_at = None
                await self.wake.wait()
                continue
            self.next_fire_at = self.scheduler.next_fire_time(datetime.now())
            if self.next_fire_at is None:
                event(log, "no allowed interrogation time in the next 8 days; check quiet_hours", logging.WARNING)
                await self.wake.wait()
                continue
            event(log, "next interrogation scheduled", at=self.next_fire_at.isoformat(timespec="seconds"))
            delay = max(0.0, (self.next_fire_at - datetime.now()).total_seconds())
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=delay)
                continue  # state changed: recompute
            except TimeoutError:
                pass
            # Re-check: the machine may have slept through quiet hours.
            if not self.scheduler.is_allowed(datetime.now()):
                continue
            ok, msg = self.trigger("scheduled")
            if not ok:
                event(log, "scheduled interrogation skipped", reason=msg)
                self._emit("skipped", {"reason": msg})
            elif self.session_task is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await asyncio.shield(self.session_task)

    # --- HTTP --------------------------------------------------------------
    def _token(self) -> str:
        return os.environ.get(self.cfg.server.token_env, "")

    @web.middleware
    async def auth_middleware(self, request: web.Request, handler: Any) -> web.StreamResponse:
        token = self._token()
        if token:
            supplied = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            supplied = supplied or request.query.get("token", "")
            if not hmac.compare_digest(supplied, token):
                return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)

    def make_app(self) -> web.Application:
        app = web.Application(middlewares=[self.auth_middleware])
        r = app.router
        r.add_get("/api/status", self.h_status)
        r.add_post("/api/trigger", self.h_trigger)
        r.add_post("/api/pause", self.h_pause)
        r.add_post("/api/resume", self.h_resume)
        r.add_post("/api/kill", self.h_kill)
        r.add_post("/api/revive", self.h_revive)
        r.add_post("/api/shutdown", self.h_shutdown)
        r.add_get("/api/stats", self.h_stats)
        r.add_get("/api/history", self.h_history)
        r.add_get("/api/events", self.h_events)
        r.add_get("/api/config", self.h_config)
        r.add_get("/ws/audio", self.h_ws_audio)
        return app

    async def h_status(self, request: web.Request) -> web.Response:
        data = self.status()
        if request.query.get("check"):
            data["llm_health"] = await self.llm.health()
        return web.json_response(data)

    async def h_trigger(self, request: web.Request) -> web.Response:
        body = await request.json() if request.can_read_body else {}
        ok, msg = self.trigger("manual", body.get("personality"))
        return web.json_response({"ok": ok, "message": msg, "seq": self.events[-1]["seq"] if self.events else 0},
                                 status=200 if ok else 409)

    async def h_pause(self, request: web.Request) -> web.Response:
        self.set_paused(True)
        return web.json_response({"ok": True, "message": "paused; no scheduled interrogations"})

    async def h_resume(self, request: web.Request) -> web.Response:
        self.set_paused(False)
        return web.json_response({"ok": True, "message": "resumed"})

    async def h_kill(self, request: web.Request) -> web.Response:
        await self.kill()
        return web.json_response({"ok": True, "message": "kill switch engaged; all audio stopped"})

    async def h_revive(self, request: web.Request) -> web.Response:
        self.revive()
        return web.json_response({"ok": True, "message": "kill switch released"})

    async def h_shutdown(self, request: web.Request) -> web.Response:
        self.shutdown_event.set()
        return web.json_response({"ok": True, "message": "shutting down"})

    async def h_stats(self, request: web.Request) -> web.Response:
        return web.json_response(self.store.stats())

    async def h_history(self, request: web.Request) -> web.Response:
        limit = max(1, min(200, int(request.query.get("limit", "20"))))
        return web.json_response(self.store.history(limit))

    async def h_events(self, request: web.Request) -> web.Response:
        since = int(request.query.get("since", "0"))
        return web.json_response([e for e in self.events if e["seq"] > since])

    async def h_config(self, request: web.Request) -> web.Response:
        return web.json_response({"source": self.cfg.source_path, "config": self.cfg.to_dict()})

    async def h_ws_audio(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        conn: ClientConn | None = None
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                data = msg.json()
                kind = data.get("type")
                if kind == "hello":
                    if self.hub.client is not None and self.hub.client is not conn:
                        old = self.hub.client
                        self.hub.drop(old)
                        with contextlib.suppress(Exception):
                            await old.ws.close()
                    conn = ClientConn(ws=ws, name=str(data.get("name", "unknown")))
                    self.hub.client = conn
                    event(log, "audio client connected", name=conn.name, peer=request.remote)
                    self._emit("audio_client", {"connected": True, "name": conn.name})
                    self.wake.set()
                elif kind == "result" and conn is not None:
                    self.hub.handle_result(conn, data)
                elif kind == "hushed":
                    event(log, "audio client hushed locally; aborting interrogation")
                    await self.abort_session()
        finally:
            if conn is not None:
                self.hub.drop(conn)
                event(log, "audio client disconnected", name=conn.name)
                self._emit("audio_client", {"connected": False, "name": conn.name})
        return ws

    # --- lifecycle ---------------------------------------------------------
    async def serve(self) -> None:
        app = self.make_app()
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, self.cfg.server.host, self.cfg.server.port)
        await site.start()
        if not self._token() and self.cfg.server.host not in ("127.0.0.1", "localhost", "::1"):
            event(log, f"listening on a non-loopback address without ${self.cfg.server.token_env} set",
                  logging.WARNING)
        event(log, "middlemanagementd ready", host=self.cfg.server.host, port=self.cfg.server.port,
              paused=self.paused, killswitch=self.killed)

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self.shutdown_event.set)

        sched = asyncio.create_task(self.scheduler_loop())
        try:
            await self.shutdown_event.wait()
        finally:
            event(log, "shutting down")
            sched.cancel()
            with contextlib.suppress(Exception):
                await self.audio.stop()
            await self.abort_session()
            with contextlib.suppress(asyncio.CancelledError):
                await sched
            await runner.cleanup()
            await self.llm.close()
            self.store.close()
