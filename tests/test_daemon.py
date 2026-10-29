"""Daemon + real AudioClient over a real WebSocket, with a fake audio engine."""

import asyncio

import aiohttp
from aiohttp.test_utils import TestServer

from middlemanagementd.audio.client import AudioClient
from middlemanagementd.audio.port import ListenRequest, ListenResult, SayResult
from middlemanagementd.daemon import Daemon

from .conftest import JARGON


class FakeEngine:
    def __init__(self, answer: str = JARGON, listen_delay: float = 0.0) -> None:
        self.answer = answer
        self.listen_delay = listen_delay
        self.said: list[str] = []
        self.kills = 0
        self.listening = asyncio.Event()

    async def chime(self) -> None:
        pass

    async def say(self, text: str, *, length_scale=None, barge_in=None, gain=1.0) -> SayResult:
        self.said.append(text)
        return SayResult()

    async def hold_music(self, max_seconds: float) -> None:
        await asyncio.sleep(max_seconds)

    async def listen(self, req: ListenRequest) -> ListenResult:
        self.listening.set()
        await asyncio.sleep(self.listen_delay)
        return ListenResult(text=self.answer, speech_seconds=5)

    def kill_all(self) -> int:
        self.kills += 1
        return 0


async def start(cfg, engine):
    cfg.llm.enabled = False
    cfg.scheduler.enabled = False
    daemon = Daemon(cfg)
    server = TestServer(daemon.make_app())
    await server.start_server()
    client = AudioClient(cfg, engine, str(server.make_url("/")), name="test-client")
    client_task = asyncio.create_task(client.run())
    for _ in range(100):
        if daemon.hub.connected:
            break
        await asyncio.sleep(0.02)
    assert daemon.hub.connected
    return daemon, server, client, client_task


async def stop(server, client, client_task):
    client.stopping.set()
    if client.ws is not None:
        await client.ws.close()
    await asyncio.wait_for(client_task, 5)
    await server.close()


async def test_session_over_websocket(cfg):
    engine = FakeEngine()
    daemon, server, client, task = await start(cfg, engine)
    try:
        ok, _ = daemon.trigger("manual")
        assert ok
        await asyncio.wait_for(daemon.session_task, 10)
        kinds = [e["type"] for e in daemon.events]
        assert "session_finished" in kinds, kinds
        assert engine.said[0]  # question spoken on the client
        assert daemon.store.stats()["successful_reports"] == 1
        # No double interrogations.
        ok, msg = daemon.trigger("manual")
        assert ok
        ok2, msg2 = daemon.trigger("manual")
        assert not ok2 and "in progress" in msg2
        await asyncio.wait_for(daemon.session_task, 10)
    finally:
        await stop(server, client, task)


async def test_kill_switch_stops_everything(cfg):
    engine = FakeEngine(listen_delay=30)
    daemon, server, client, task = await start(cfg, engine)
    try:
        assert daemon.trigger("manual")[0]
        await asyncio.wait_for(engine.listening.wait(), 5)
        async with aiohttp.ClientSession() as http:
            async with http.post(server.make_url("/api/kill")) as r:
                assert (await r.json())["ok"]
            assert not daemon.busy
            assert engine.kills >= 1
            assert not client.ops  # client-side op tasks cancelled
            assert any(e["type"] == "session_aborted" for e in daemon.events)
            async with http.post(server.make_url("/api/trigger")) as r:
                assert r.status == 409 and "kill switch" in (await r.json())["message"]
            async with http.post(server.make_url("/api/revive")) as r:
                assert (await r.json())["ok"]
            async with http.get(server.make_url("/api/status")) as r:
                assert (await r.json())["killswitch"] is False
        assert daemon.store.stats()["total_interrogations"] == 0
    finally:
        await stop(server, client, task)


async def test_local_hush_aborts_session(cfg):
    engine = FakeEngine(listen_delay=30)
    daemon, server, client, task = await start(cfg, engine)
    try:
        assert daemon.trigger("manual")[0]
        await asyncio.wait_for(engine.listening.wait(), 5)
        client.hush()
        for _ in range(100):
            if not daemon.busy:
                break
            await asyncio.sleep(0.02)
        assert not daemon.busy
    finally:
        await stop(server, client, task)


async def test_trigger_refused_without_audio_client(cfg):
    cfg.llm.enabled = False
    daemon = Daemon(cfg)
    ok, msg = daemon.trigger("manual")
    assert not ok and "no audio client" in msg


async def test_token_auth(cfg, monkeypatch):
    monkeypatch.setenv("MMD_TOKEN", "s3cret")
    cfg.llm.enabled = False
    daemon = Daemon(cfg)
    server = TestServer(daemon.make_app())
    await server.start_server()
    try:
        async with aiohttp.ClientSession() as http:
            async with http.get(server.make_url("/api/status")) as r:
                assert r.status == 401
            async with http.get(server.make_url("/api/status"), headers={"Authorization": "Bearer s3cret"}) as r:
                assert r.status == 200
    finally:
        await server.close()
