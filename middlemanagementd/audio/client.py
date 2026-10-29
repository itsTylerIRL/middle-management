"""Audio client: runs on the machine with the microphone and speakers, executes ops for the daemon."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import socket
from pathlib import Path
from typing import Any

import aiohttp

from .. import __version__
from ..config import Config
from ..logs import event
from .engine import AudioEngine
from .port import AudioSilenced, AudioUnavailable, ListenRequest

log = logging.getLogger(__name__)


def pidfile_path() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/middlemanagementd-{os.getuid()}"
    Path(runtime).mkdir(parents=True, exist_ok=True)
    return Path(runtime) / "middlemanagementd-audio.pid"


class AudioClient:
    def __init__(self, cfg: Config, engine: AudioEngine, url: str, token: str = "", name: str | None = None) -> None:
        self.cfg = cfg
        self.engine = engine
        self.ws_url = url.rstrip("/").replace("http://", "ws://").replace("https://", "wss://") + "/ws/audio"
        self.token = token
        self.name = name or socket.gethostname()
        self.ops: dict[int, asyncio.Task[None]] = {}
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self.stopping = asyncio.Event()

    # --- silencing -----------------------------------------------------------
    def hush(self) -> None:
        """Local panic button: stop every sound right now, no daemon required."""
        for task in list(self.ops.values()):
            task.cancel()
        self.engine.kill_all()
        if self.ws is not None and not self.ws.closed:
            asyncio.ensure_future(self._send({"type": "hushed"}))
        event(log, "hushed locally")

    async def _send(self, msg: dict[str, Any]) -> None:
        if self.ws is not None and not self.ws.closed:
            with contextlib.suppress(Exception):
                await self.ws.send_json(msg)

    # --- op execution --------------------------------------------------------
    async def _execute(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        e = self.engine
        if op == "chime":
            await e.chime()
            return {}
        if op == "say":
            await e.say(str(args["text"]), length_scale=args.get("length_scale"))
            return {}
        if op == "hold_music":
            await e.hold_music(float(args.get("max_seconds", 60)))
            return {}
        if op == "listen":
            req = ListenRequest(**{k: v for k, v in args.items() if k in ListenRequest.__dataclass_fields__})
            return (await e.listen(req)).to_dict()
        raise ValueError(f"unknown op {op!r}")

    async def _run_op(self, rid: int, op: str, args: dict[str, Any]) -> None:
        reply: dict[str, Any] = {"type": "result", "id": rid}
        try:
            reply.update(ok=True, data=await self._execute(op, args))
        except asyncio.CancelledError:
            reply.update(ok=False, error="cancelled", kind="silenced")
        except AudioSilenced as ex:
            reply.update(ok=False, error=str(ex), kind="silenced")
        except AudioUnavailable as ex:
            event(log, "audio op failed", logging.WARNING, op=op, error=str(ex))
            reply.update(ok=False, error=str(ex), kind="unavailable")
        except Exception as ex:  # noqa: BLE001 - report every failure back to the daemon
            log.exception("audio op %s crashed", op)
            reply.update(ok=False, error=repr(ex), kind="error")
        finally:
            self.ops.pop(rid, None)
        await self._send(reply)

    async def _session(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        self.ws = ws
        await ws.send_json({"type": "hello", "name": self.name, "version": __version__})
        event(log, "connected to daemon", url=self.ws_url)
        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            data = msg.json()
            kind = data.get("type")
            if kind == "op":
                rid = int(data["id"])
                self.ops[rid] = asyncio.create_task(self._run_op(rid, data["op"], data.get("args") or {}))
            elif kind == "cancel":
                if task := self.ops.get(int(data.get("id", -1))):
                    task.cancel()
            elif kind == "stop":
                event(log, "stop received from daemon")
                for task in list(self.ops.values()):
                    task.cancel()
                self.engine.kill_all()

    async def run(self) -> None:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        backoff = 1.0
        async with aiohttp.ClientSession(headers=headers) as http:
            while not self.stopping.is_set():
                try:
                    async with http.ws_connect(self.ws_url, heartbeat=20, timeout=aiohttp.ClientWSTimeout(ws_close=5)) as ws:
                        backoff = 1.0
                        await self._session(ws)
                except (aiohttp.ClientError, OSError) as e:
                    event(log, "daemon unreachable", logging.WARNING, url=self.ws_url, error=str(e)[:120],
                          retry_in=backoff)
                finally:
                    # Losing the daemon means nobody can tell us to stop, so stop now.
                    self.ws = None
                    for task in list(self.ops.values()):
                        task.cancel()
                    self.engine.kill_all()
                if self.stopping.is_set():
                    break
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.stopping.wait(), backoff)
                backoff = min(backoff * 2, 30.0)

    async def main(self) -> None:
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGUSR1, self.hush)
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._shutdown)
        pid = pidfile_path()
        pid.write_text(str(os.getpid()))
        try:
            await self.run()
        finally:
            self.engine.kill_all()
            with contextlib.suppress(FileNotFoundError):
                pid.unlink()

    def _shutdown(self) -> None:
        self.engine.kill_all()
        self.stopping.set()
        if self.ws is not None:
            asyncio.ensure_future(self.ws.close())
