"""Playback and capture. Two backends behind one interface:

* pulse: PulseAudio/PipeWire command-line tools (paplay / parecord), the default on Linux. Every sound is a
  subprocess, so the kill switch is just SIGKILL: nothing can keep talking.
* portaudio: sounddevice callback streams, for macOS and Windows (and Linux without paplay). Every stream checks a
  kill flag on each audio callback and aborts, so the kill switch is just as final.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

import numpy as np

from ..logs import event
from .port import AudioSilenced, AudioUnavailable

log = logging.getLogger(__name__)

CHUNK_SECONDS = 0.05
# Never write more than this far ahead of real time: keeps echo timing predictable, makes gain changes
# audible quickly, and means a kill never has seconds of audio already queued.
LEAD_SECONDS = 0.25


BACKENDS = ("auto", "pulse", "portaudio")


def resolve_backend(name: str) -> str:
    """'auto' means pulse on Linux when its tools are installed, PortAudio everywhere else."""
    if name != "auto":
        return name
    pulse = sys.platform.startswith("linux") and shutil.which("paplay") and shutil.which("parecord")
    return "pulse" if pulse else "portaudio"


def list_devices(kind: str, backend: str = "pulse") -> list[str]:
    """kind: 'sinks' or 'sources'. Returns device names (empty if the backend can't enumerate them)."""
    if backend == "portaudio":
        return portaudio_devices(kind)
    if not shutil.which("pactl"):
        return []
    try:
        out = subprocess.run(["pactl", "list", "short", kind], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [line.split("\t")[1] for line in out.splitlines() if "\t" in line]


def check_device(name: str, kind: str) -> None:
    tool = "paplay" if kind == "sinks" else "parecord"
    if not shutil.which(tool):
        raise AudioUnavailable(f"{tool} not found; install libpulse / pipewire-pulse")
    if name and name != "default" and name not in list_devices(kind):
        raise AudioUnavailable(f"audio device {name!r} not found among {kind}")


@dataclass
class GainControl:
    value: float


def to_int16(samples: np.ndarray, gain: float) -> bytes:
    return (np.clip(samples * gain, -1.0, 1.0) * 32767).astype("<i2").tobytes()


class ProcessRegistry:
    """Tracks every live audio subprocess and stream so they can all be killed at once."""

    def __init__(self) -> None:
        self.procs: set[asyncio.subprocess.Process] = set()
        self.streams: set[threading.Event] = set()  # set = abort that PortAudio stream

    def kill_all(self) -> int:
        n = 0
        for p in list(self.procs):
            if p.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    p.kill()
                    n += 1
        self.procs.clear()
        for killed in list(self.streams):
            killed.set()
            n += 1
        self.streams.clear()
        return n

    @contextlib.asynccontextmanager
    async def spawn(self, *args: str, stdin: bool = False, stdout: bool = False) -> AsyncIterator[asyncio.subprocess.Process]:
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE if stdin else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE if stdout else asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as e:
            raise AudioUnavailable(f"{args[0]} not found") from e
        self.procs.add(proc)
        try:
            yield proc
        finally:
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), 2)
            self.procs.discard(proc)


class Player:
    def __init__(self, registry: ProcessRegistry, device: str = "default") -> None:
        self.registry = registry
        self.device = device

    def _cmd(self, rate: int) -> list[str]:
        cmd = ["paplay", "--raw", "--format=s16le", f"--rate={rate}", "--channels=1",
               "--latency-msec=60", "--client-name=middlemanagementd", "--stream-name=managerial feedback"]
        if self.device and self.device != "default":
            cmd.append(f"--device={self.device}")
        return cmd

    def check(self) -> None:
        check_device(self.device, "sinks")

    async def play(self, samples: np.ndarray, rate: int, gain: float | GainControl, *,
                   loop_seconds: float | None = None, on_start: Callable[[], None] | None = None) -> None:
        """Play float32 mono samples. With loop_seconds, repeat until that much audio has been played.

        `gain` may be a GainControl, which is re-read for every chunk so playback can get louder mid-sentence.
        """
        check_device(self.device, "sinks")
        ctl = gain if isinstance(gain, GainControl) else GainControl(gain)
        if len(samples) == 0:
            return
        chunk = max(1, int(rate * CHUNK_SECONDS))
        total = len(samples) if loop_seconds is None else int(loop_seconds * rate)
        async with self.registry.spawn(*self._cmd(rate), stdin=True) as proc:
            assert proc.stdin is not None
            written = 0
            started = 0.0
            try:
                while written < total:
                    off = written % len(samples)
                    piece = samples[off : off + min(chunk, total - written, len(samples) - off)]
                    proc.stdin.write(to_int16(piece, ctl.value))
                    if written == 0:
                        started = time.monotonic()
                        if on_start is not None:
                            on_start()
                    await proc.stdin.drain()
                    written += len(piece)
                    ahead = written / rate - (time.monotonic() - started)
                    if ahead > LEAD_SECONDS:
                        await asyncio.sleep(ahead - LEAD_SECONDS)
                proc.stdin.close()
                rc = await proc.wait()
            except (BrokenPipeError, ConnectionResetError):
                rc = await proc.wait()
            if rc is not None and rc < 0:
                raise AudioSilenced("playback killed")
            if rc:
                err = (await proc.stderr.read()).decode(errors="replace").strip() if proc.stderr else ""
                raise AudioUnavailable(f"paplay exited {rc}: {err[:200]}")


class Recorder:
    def __init__(self, registry: ProcessRegistry, device: str = "default", rate: int = 16000) -> None:
        self.registry = registry
        self.device = device
        self.rate = rate

    def _cmd(self) -> list[str]:
        cmd = ["parecord", "--raw", "--format=s16le", f"--rate={self.rate}", "--channels=1",
               "--latency-msec=30", "--client-name=middlemanagementd", "--stream-name=status report"]
        if self.device and self.device != "default":
            cmd.append(f"--device={self.device}")
        return cmd

    def check(self) -> None:
        check_device(self.device, "sources")

    async def frames(self, frame_ms: int = 30) -> AsyncIterator[np.ndarray]:
        """Yield float32 frames until the consumer stops iterating."""
        check_device(self.device, "sources")
        nbytes = self.rate * frame_ms // 1000 * 2
        async with self.registry.spawn(*self._cmd(), stdout=True) as proc:
            assert proc.stdout is not None
            while True:
                try:
                    data = await proc.stdout.readexactly(nbytes)
                except asyncio.IncompleteReadError:
                    rc = await proc.wait()
                    if rc is not None and rc < 0:
                        raise AudioSilenced("recording killed") from None
                    err = (await proc.stderr.read()).decode(errors="replace").strip() if proc.stderr else ""
                    event(log, "microphone stream ended", logging.WARNING, rc=rc, err=err[:200])
                    raise AudioUnavailable(f"microphone unavailable (parecord exited {rc}): {err[:200]}") from None
                yield np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0


# --- PortAudio (sounddevice) -------------------------------------------------------------------------------------
def _sounddevice():  # noqa: ANN202
    try:
        import sounddevice  # type: ignore[import-not-found]
    except (ImportError, OSError) as e:  # OSError: the PortAudio library itself is missing
        raise AudioUnavailable(f"PortAudio unavailable ({e}); pip install 'middlemanagementd[audio]'") from e
    return sounddevice


def _channels_key(kind: str) -> str:
    return "max_output_channels" if kind == "sinks" else "max_input_channels"


def portaudio_devices(kind: str) -> list[str]:
    try:
        sd = _sounddevice()
        return [d["name"] for d in sd.query_devices() if d[_channels_key(kind)] > 0]
    except Exception:  # noqa: BLE001 - enumeration is best effort
        return []


def portaudio_device(name: str, kind: str) -> int | None:
    """None for the system default; otherwise the first device whose name contains `name`."""
    if not name or name == "default":
        _sounddevice()
        return None
    sd = _sounddevice()
    for i, d in enumerate(sd.query_devices()):
        if name.lower() in d["name"].lower() and d[_channels_key(kind)] > 0:
            return i
    raise AudioUnavailable(f"audio device {name!r} not found among {portaudio_devices(kind)}")


def device_rate(sd, device: int | None, kind: str) -> int:  # noqa: ANN001
    """The rate the device actually runs at. Hardware often refuses others (16 kHz, Piper's 22.05 kHz)."""
    return int(sd.query_devices(device, "output" if kind == "sinks" else "input")["default_samplerate"])


def resample(samples: np.ndarray, src: int, dst: int, n: int | None = None) -> np.ndarray:
    """Linear resampling; plenty for speech, chimes and elevator jazz. `n` fixes the output length."""
    if src == dst and (n is None or n == len(samples)):
        return samples
    n = n if n is not None else max(1, round(len(samples) * dst / src))
    return np.interp(np.linspace(0, len(samples) - 1, n), np.arange(len(samples)), samples).astype(np.float32)


class PortAudioPlayer:
    def __init__(self, registry: ProcessRegistry, device: str = "default") -> None:
        self.registry = registry
        self.device = device

    def check(self) -> None:
        portaudio_device(self.device, "sinks")

    async def play(self, samples: np.ndarray, rate: int, gain: float | GainControl, *,
                   loop_seconds: float | None = None, on_start: Callable[[], None] | None = None) -> None:
        """Same contract as Player.play. Gain is read on every audio callback."""
        sd = _sounddevice()
        device = portaudio_device(self.device, "sinks")
        ctl = gain if isinstance(gain, GainControl) else GainControl(gain)
        if len(samples) == 0:
            return
        out_rate = device_rate(sd, device, "sinks")
        samples, rate = resample(samples, rate, out_rate), out_rate
        total = len(samples) if loop_seconds is None else int(loop_seconds * rate)
        loop = asyncio.get_running_loop()
        done = asyncio.Event()
        killed = threading.Event()
        pos = 0

        def callback(outdata: np.ndarray, frames: int, _time: object, _status: object) -> None:
            nonlocal pos
            if killed.is_set():
                raise sd.CallbackAbort
            if pos == 0 and on_start is not None:
                loop.call_soon_threadsafe(on_start)
            n = min(frames, total - pos)
            idx = np.arange(pos, pos + n) % len(samples)
            outdata[:n, 0] = np.clip(samples[idx] * ctl.value, -1.0, 1.0)
            outdata[n:, 0] = 0.0
            pos += n
            if pos >= total:
                raise sd.CallbackStop

        def finished() -> None:
            with contextlib.suppress(RuntimeError):  # the loop may already be gone at shutdown
                loop.call_soon_threadsafe(done.set)

        self.registry.streams.add(killed)
        try:
            with sd.OutputStream(samplerate=rate, channels=1, dtype="float32", device=device, latency="low",
                                 callback=callback, finished_callback=finished):
                await done.wait()
        except sd.PortAudioError as e:
            raise AudioUnavailable(f"audio output failed: {e}") from e
        finally:
            self.registry.streams.discard(killed)
        if killed.is_set():
            raise AudioSilenced("playback killed")


class PortAudioRecorder:
    def __init__(self, registry: ProcessRegistry, device: str = "default", rate: int = 16000) -> None:
        self.registry = registry
        self.device = device
        self.rate = rate

    def check(self) -> None:
        portaudio_device(self.device, "sources")

    async def frames(self, frame_ms: int = 30) -> AsyncIterator[np.ndarray]:
        """Same contract as Recorder.frames."""
        sd = _sounddevice()
        device = portaudio_device(self.device, "sources")
        in_rate = device_rate(sd, device, "sources")
        frame = self.rate * frame_ms // 1000
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[np.ndarray | None] = asyncio.Queue()
        killed = threading.Event()

        def deliver(item: np.ndarray | None) -> None:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(queue.put_nowait, item)

        def callback(indata: np.ndarray, _frames: int, _time: object, _status: object) -> None:
            if killed.is_set():
                raise sd.CallbackAbort
            deliver(resample(indata[:, 0].copy(), in_rate, self.rate, frame))

        self.registry.streams.add(killed)
        try:
            with sd.InputStream(samplerate=in_rate, channels=1, dtype="float32", device=device,
                                blocksize=in_rate * frame_ms // 1000, callback=callback,
                                finished_callback=lambda: deliver(None)):
                while (block := await queue.get()) is not None:
                    yield block
            if killed.is_set():
                raise AudioSilenced("recording killed")
            raise AudioUnavailable("microphone stream ended")
        except sd.PortAudioError as e:
            raise AudioUnavailable(f"microphone unavailable: {e}") from e
        finally:
            self.registry.streams.discard(killed)


def make_player(backend: str, registry: ProcessRegistry, device: str) -> Player | PortAudioPlayer:
    return PortAudioPlayer(registry, device) if backend == "portaudio" else Player(registry, device)


def make_recorder(backend: str, registry: ProcessRegistry, device: str) -> Recorder | PortAudioRecorder:
    return PortAudioRecorder(registry, device) if backend == "portaudio" else Recorder(registry, device)


class EchoCancelModule:
    """PipeWire/PulseAudio echo cancellation, loaded at runtime and unloaded on exit. Nothing is persisted."""

    SINK = "mmd_ec_sink"
    SOURCE = "mmd_ec_source"

    def __init__(self, sink_master: str = "default", source_master: str = "default") -> None:
        self.sink_master = sink_master
        self.source_master = source_master
        self.index: str | None = None

    @staticmethod
    def _pactl(*args: str) -> str:
        try:
            res = subprocess.run(["pactl", *args], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise AudioUnavailable(f"pactl failed: {e}") from e
        if res.returncode:
            raise AudioUnavailable(f"pactl {' '.join(args[:2])} failed: {res.stderr.strip()[:200]}")
        return res.stdout.strip()

    @classmethod
    def unload_stale(cls) -> None:
        """Remove modules left behind by a client that was killed before it could clean up."""
        for line in cls._pactl("list", "short", "modules").splitlines():
            parts = line.split("\t")
            if len(parts) >= 3 and parts[1] == "module-echo-cancel" and cls.SINK in parts[2]:
                cls._pactl("unload-module", parts[0])

    def load(self) -> tuple[str, str]:
        self.unload_stale()
        sink = self.sink_master if self.sink_master != "default" else self._pactl("get-default-sink")
        source = self.source_master if self.source_master != "default" else self._pactl("get-default-source")
        self.index = self._pactl(
            "load-module", "module-echo-cancel", "aec_method=webrtc", f"sink_master={sink}",
            f"source_master={source}", f"sink_name={self.SINK}", f"source_name={self.SOURCE}",
        )
        event(log, "echo cancellation loaded", module=self.index, sink=sink, source=source)
        return self.SINK, self.SOURCE

    def unload(self) -> None:
        if self.index is None:
            return
        with contextlib.suppress(AudioUnavailable):
            self._pactl("unload-module", self.index)
            event(log, "echo cancellation unloaded", module=self.index)
        self.index = None
