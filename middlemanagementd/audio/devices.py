"""Playback and capture through PulseAudio/PipeWire command-line tools (paplay / parecord).

Every sound is a subprocess, so the kill switch is just SIGKILL: nothing can keep talking.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import subprocess
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


def list_devices(kind: str) -> list[str]:
    """kind: 'sinks' or 'sources'. Returns PulseAudio device names (empty if pactl is unusable)."""
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
    """Tracks every live audio subprocess so they can all be killed at once."""

    def __init__(self) -> None:
        self.procs: set[asyncio.subprocess.Process] = set()

    def kill_all(self) -> int:
        n = 0
        for p in list(self.procs):
            if p.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    p.kill()
                    n += 1
        self.procs.clear()
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
