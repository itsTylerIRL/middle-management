"""Playback and capture through PulseAudio/PipeWire command-line tools (paplay / parecord).

Every sound is a subprocess, so the kill switch is just SIGKILL: nothing can keep talking.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import subprocess
from collections.abc import AsyncIterator

import numpy as np

from ..logs import event
from .port import AudioSilenced, AudioUnavailable

log = logging.getLogger(__name__)

CHUNK_SECONDS = 0.25


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
               "--client-name=middlemanagementd", "--stream-name=managerial feedback"]
        if self.device and self.device != "default":
            cmd.append(f"--device={self.device}")
        return cmd

    async def play(self, samples: np.ndarray, rate: int, gain: float, *, loop_seconds: float | None = None) -> None:
        """Play float32 mono samples. With loop_seconds, repeat until that much audio has been played."""
        check_device(self.device, "sinks")
        pcm = to_int16(samples, gain)
        bytes_per_sec = rate * 2
        chunk = int(bytes_per_sec * CHUNK_SECONDS) & ~1
        total = len(pcm) if loop_seconds is None else int(loop_seconds * bytes_per_sec) & ~1
        async with self.registry.spawn(*self._cmd(rate), stdin=True) as proc:
            assert proc.stdin is not None
            written = 0
            try:
                while written < total:
                    off = written % len(pcm)
                    piece = pcm[off : off + min(chunk, total - written)]
                    proc.stdin.write(piece)
                    await proc.stdin.drain()
                    written += len(piece)
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
