"""Audio client: runs on the machine with the microphone and speakers, executes ops for the daemon."""

from __future__ import annotations

import asyncio
import contextlib
import getpass
import hmac
import json
import logging
import os
import secrets
import signal
import socket
import tempfile
from pathlib import Path
from typing import Any

import aiohttp

from .. import __version__
from ..config import Config
from ..logs import event
from .engine import AudioEngine
from .port import AudioSilenced, AudioUnavailable, BargeIn, ListenRequest

log = logging.getLogger(__name__)


def state_path() -> Path:
    """Where a running audio client advertises its control port: {"pid", "port", "token"}."""
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()) / f"middlemanagementd-{getpass.getuser()}"
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    return runtime / "audio-client.json"


def control(command: str, timeout: float = 2.0) -> bool:
    """Send hush / stop / ping to the local audio client. False if none is running."""
    try:
        state = json.loads(state_path().read_text())
        with socket.create_connection(("127.0.0.1", int(state["port"])), timeout=timeout) as s:
            s.sendall(f"{state['token']} {command}\n".encode())
            return s.recv(8).startswith(b"ok")
    except (OSError, ValueError, KeyError, TypeError):
        return False


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
        self._token = secrets.token_hex(16)

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
            barge_in = BargeIn.from_dict(args["barge_in"]) if args.get("barge_in") else None
            res = await e.say(str(args["text"]), length_scale=args.get("length_scale"), barge_in=barge_in,
                              gain=float(args.get("gain", 1.0)))
            return res.to_dict()
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

    async def _control(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Local control channel (`hush`, `stop`, `ping`): the same on every OS, unlike signals."""
        try:
            token, _, command = (await asyncio.wait_for(reader.readline(), 2)).decode().strip().partition(" ")
            if hmac.compare_digest(token, self._token) and command in ("hush", "stop", "ping"):
                {"hush": self.hush, "stop": self._shutdown, "ping": lambda: None}[command]()
                writer.write(b"ok\n")
                await writer.drain()
        except (OSError, TimeoutError, UnicodeDecodeError):
            pass
        finally:
            writer.close()

    async def main(self) -> None:
        loop = asyncio.get_running_loop()
        for sig, handler in ((getattr(signal, "SIGUSR1", None), self.hush), (signal.SIGINT, self._shutdown),
                             (signal.SIGTERM, self._shutdown)):
            # Not available on Windows; the control socket covers it there.
            with contextlib.suppress(NotImplementedError, AttributeError, RuntimeError, TypeError):
                loop.add_signal_handler(sig, handler)
        server = await asyncio.start_server(self._control, "127.0.0.1", 0)
        state = state_path()
        fd = os.open(state, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"pid": os.getpid(), "port": server.sockets[0].getsockname()[1], "token": self._token}, f)
        try:
            await self.run()
        finally:
            server.close()
            self.engine.close()
            with contextlib.suppress(FileNotFoundError):
                state.unlink()

    def _shutdown(self) -> None:
        self.engine.kill_all()
        self.stopping.set()
        if self.ws is not None:
            asyncio.ensure_future(self.ws.close())
