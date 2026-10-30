"""Cross-platform pieces: the PortAudio backend (against a fake sounddevice), backend choice, the audio client's
control socket, and the macOS / Windows system voices (against fake subprocesses)."""

import asyncio
import io
import json
import sys
import threading
import types
import wave
from pathlib import Path

import numpy as np
import pytest

from middlemanagementd.audio import devices
from middlemanagementd.audio.devices import (GainControl, PortAudioPlayer, PortAudioRecorder, ProcessRegistry,
                                             resolve_backend)
from middlemanagementd.audio.port import AudioSilenced


class FakeStream:
    """Calls the callback from a thread, like PortAudio, until it stops or aborts."""

    def __init__(self, sd, kind, *, samplerate, channels, dtype, device, callback, finished_callback,
                 blocksize=0, latency=None):
        self.sd, self.kind, self.callback, self.finished = sd, kind, callback, finished_callback
        self.blocksize = blocksize or 256
        self.stop = threading.Event()
        sd.opened.append(self)

    def _run(self):
        try:
            while not self.stop.is_set():
                buf = np.zeros((self.blocksize, 1), dtype=np.float32)
                if self.kind == "in":
                    buf[:] = 0.25
                try:
                    self.callback(buf, self.blocksize, None, None)
                except self.sd.CallbackStop:
                    if self.kind == "out":  # PortAudio still plays the buffer that raised CallbackStop
                        self.sd.played.append(buf[:, 0].copy())
                    raise
                if self.kind == "out":
                    self.sd.played.append(buf[:, 0].copy())
                self.stop.wait(0.001)
        except (self.sd.CallbackStop, self.sd.CallbackAbort) as e:
            self.sd.ended.append(type(e).__name__)
        self.finished()

    def __enter__(self):
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join(2)


@pytest.fixture
def fake_sd(monkeypatch):
    sd = types.SimpleNamespace(opened=[], played=[], ended=[])
    sd.CallbackStop = type("CallbackStop", (Exception,), {})
    sd.CallbackAbort = type("CallbackAbort", (Exception,), {})
    sd.PortAudioError = type("PortAudioError", (Exception,), {})
    listing = [{"name": "MacBook Pro Speakers", "max_output_channels": 2, "max_input_channels": 0},
               {"name": "MacBook Pro Microphone", "max_output_channels": 0, "max_input_channels": 1}]
    # Like real hardware: everything runs at 48 kHz, whatever rate the audio was made at.
    sd.query_devices = lambda device=None, kind=None: listing if kind is None else {"default_samplerate": 48000.0}
    sd.OutputStream = lambda **kw: FakeStream(sd, "out", **kw)
    sd.InputStream = lambda **kw: FakeStream(sd, "in", **kw)
    monkeypatch.setattr(devices, "_sounddevice", lambda: sd)
    return sd


async def test_portaudio_plays_everything_with_live_gain_and_on_start(fake_sd):
    started = []
    ctl = GainControl(0.5)
    player = PortAudioPlayer(ProcessRegistry(), "Speakers")
    await player.play(np.ones(1000, dtype=np.float32), 16000, ctl, on_start=lambda: started.append(1))
    played = np.concatenate(fake_sd.played)
    assert started == [1] and fake_sd.ended == ["CallbackStop"]
    assert np.count_nonzero(played) == 3000 and np.allclose(played[:3000], 0.5)  # 16 kHz -> 48 kHz


async def test_portaudio_loops_for_hold_music(fake_sd):
    await PortAudioPlayer(ProcessRegistry()).play(np.ones(100, dtype=np.float32), 1000, 1.0, loop_seconds=0.5)
    assert np.count_nonzero(np.concatenate(fake_sd.played)) == 24000  # half a second at 48 kHz


async def test_kill_switch_aborts_portaudio_playback(fake_sd):
    registry = ProcessRegistry()
    play = asyncio.create_task(PortAudioPlayer(registry).play(np.ones(10**7, dtype=np.float32), 16000, 1.0))
    await asyncio.sleep(0.05)
    assert registry.kill_all() == 1
    with pytest.raises(AudioSilenced):
        await asyncio.wait_for(play, 2)
    assert fake_sd.ended == ["CallbackAbort"] and not registry.streams


async def test_portaudio_recorder_frames_and_kill(fake_sd):
    registry = ProcessRegistry()
    recorder = PortAudioRecorder(registry, "Microphone")
    frames = recorder.frames(frame_ms=30)
    first = await anext(frames)
    assert first.shape == (480,) and np.allclose(first, 0.25)
    registry.kill_all()
    with pytest.raises(AudioSilenced):
        while True:
            await asyncio.wait_for(anext(frames), 2)


def test_portaudio_device_lookup(fake_sd):
    assert devices.portaudio_device("default", "sinks") is None
    assert devices.portaudio_device("speakers", "sinks") == 0
    assert devices.portaudio_device("microphone", "sources") == 1
    assert devices.list_devices("sources", "portaudio") == ["MacBook Pro Microphone"]
    with pytest.raises(devices.AudioUnavailable):
        devices.portaudio_device("microphone", "sinks")


@pytest.mark.parametrize("platform, tools, expected", [
    ("linux", True, "pulse"), ("linux", False, "portaudio"), ("darwin", True, "portaudio"),
    ("win32", False, "portaudio")])
def test_auto_backend(monkeypatch, platform, tools, expected):
    monkeypatch.setattr(devices.sys, "platform", platform)
    monkeypatch.setattr(devices.shutil, "which", lambda tool: f"/usr/bin/{tool}" if tools else None)
    assert resolve_backend("auto") == expected
    assert resolve_backend("portaudio") == "portaudio"


async def test_audio_client_control_socket(cfg, monkeypatch, tmp_path):
    from middlemanagementd.audio import client as client_mod

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    engine = types.SimpleNamespace(kills=0)
    engine.kill_all = lambda: setattr(engine, "kills", engine.kills + 1)
    c = client_mod.AudioClient(cfg, engine, "http://127.0.0.1:1")
    server = await asyncio.start_server(c._control, "127.0.0.1", 0)
    client_mod.state_path().write_text(json.dumps({"pid": 1, "port": server.sockets[0].getsockname()[1],
                                                   "token": c._token}))
    try:
        assert await asyncio.to_thread(client_mod.control, "ping")
        assert await asyncio.to_thread(client_mod.control, "hush") and engine.kills == 1
        state = json.loads(client_mod.state_path().read_text())
        client_mod.state_path().write_text(json.dumps({**state, "token": "wrong"}))
        assert not await asyncio.to_thread(client_mod.control, "hush") and engine.kills == 1
        assert await asyncio.to_thread(client_mod.control, "nonsense") is False
    finally:
        server.close()
    client_mod.state_path().unlink()
    assert not client_mod.control("ping")  # nothing running


def _wav_bytes(n=2205, rate=22050):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x10" * n)
    return buf.getvalue()


@pytest.mark.parametrize("platform", ["darwin", "win32"])
async def test_system_voice(monkeypatch, platform):
    from middlemanagementd.audio import speech
    from middlemanagementd.config import TTSConfig

    calls = []

    def fake_run(cmd, **kw):
        calls.append((cmd, kw))
        path = cmd[cmd.index("-o") + 1] if platform == "darwin" else cmd[-1].split("SetOutputToWaveFile('")[1].split("')")[0]
        Path(path).write_bytes(_wav_bytes())
        return types.SimpleNamespace(stdout=b"", returncode=0)

    monkeypatch.setattr(speech.sys, "platform", platform)
    monkeypatch.setattr(speech.shutil, "which", lambda tool: tool if tool in ("say", "powershell") else None)
    monkeypatch.setattr(speech.subprocess, "run", fake_run)
    assert speech.fallback_backend() == "system"
    tts = speech.TTS(TTSConfig(backend="system"))
    samples, rate = await tts.synthesize("Let's circle back.", length_scale=1.0)
    assert rate == 22050 and len(samples) == 2205
    cmd, kw = calls[0]
    if platform == "darwin":
        assert cmd[0] == "say" and cmd[-1] == "Let's circle back."
    else:
        assert cmd[0] == "powershell" and kw["input"] == b"Let's circle back."  # text never touches the script


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="pulse backend is Linux-only")
def test_pulse_backend_still_default_on_linux_with_tools(monkeypatch):
    monkeypatch.setattr(devices.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    assert resolve_backend("auto") == "pulse"


@pytest.mark.skipif(not hasattr(__import__("signal"), "SIGUSR1"), reason="POSIX signals only")
def test_stop_reaches_a_pre_0_3_audio_client(monkeypatch, tmp_path):
    import os
    import signal

    from middlemanagementd import cli

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    (tmp_path / "middlemanagementd-audio.pid").write_text("4242")
    sent = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: sent.append((pid, sig)))
    assert cli.audio_client_command("stop") and cli.audio_client_command("ping")
    assert sent == [(4242, signal.SIGTERM), (4242, 0)]
