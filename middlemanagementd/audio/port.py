"""The interface between the interrogation logic and whatever produces/captures sound.

Implementations:
  RemoteAudioPort  (daemon.py)  - forwards ops over WebSocket to an audio client on another machine
  LocalAudioPort               - drives an in-process AudioEngine (single-machine mode)
  ConsoleAudioPort             - prints questions, reads typed answers (rehearsal / no hardware)
  MockAudioPort                - scripted, for tests
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from .engine import AudioEngine


class AudioError(RuntimeError):
    """Audio could not be produced or captured (device missing, client gone, silenced)."""


class AudioUnavailable(AudioError):
    pass


class AudioSilenced(AudioError):
    """The kill switch or a local hush stopped this operation."""


@dataclass
class ListenRequest:
    max_seconds: float = 60.0
    start_timeout: float = 8.0
    end_silence: float = 1.8
    min_speech: float = 0.4
    interrupt_after: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ListenResult:
    text: str = ""
    speech_seconds: float = 0.0
    interrupted: bool = False
    no_speech: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AudioPort(Protocol):
    async def chime(self) -> None: ...

    async def say(self, text: str, *, length_scale: float | None = None) -> None: ...

    async def listen(self, req: ListenRequest) -> ListenResult: ...

    async def hold_music(self, max_seconds: float) -> None:
        """Play looping hold music until cancelled or max_seconds elapses."""
        ...

    async def stop(self) -> None:
        """Immediately stop all playback and capture."""
        ...


class LocalAudioPort:
    def __init__(self, engine: AudioEngine) -> None:
        self.engine = engine

    async def chime(self) -> None:
        await self.engine.chime()

    async def say(self, text: str, *, length_scale: float | None = None) -> None:
        await self.engine.say(text, length_scale=length_scale)

    async def listen(self, req: ListenRequest) -> ListenResult:
        return await self.engine.listen(req)

    async def hold_music(self, max_seconds: float) -> None:
        await self.engine.hold_music(max_seconds)

    async def stop(self) -> None:
        self.engine.kill_all()


class ConsoleAudioPort:
    """Text-only stand-in: questions are printed (optionally spoken), answers are typed."""

    def __init__(self, speaker: LocalAudioPort | None = None, printer: Any = print) -> None:
        self.speaker = speaker
        self.print = printer

    async def chime(self) -> None:
        self.print("[bold magenta]♪ ba-DING ba-DING ♪[/] [dim](corporate notification)[/]")
        if self.speaker:
            await self.speaker.chime()

    async def say(self, text: str, *, length_scale: float | None = None) -> None:
        self.print(f"[bold cyan]Manager:[/] {text}")
        if self.speaker:
            await self.speaker.say(text, length_scale=length_scale)

    async def listen(self, req: ListenRequest) -> ListenResult:
        text = await asyncio.to_thread(input, "Your status update> ")
        return ListenResult(text=text, speech_seconds=len(text.split()) / 2.5, no_speech=not text.strip())

    async def hold_music(self, max_seconds: float) -> None:
        self.print("[dim]♫ (hold music) ♫[/]")
        if self.speaker:
            await self.speaker.hold_music(max_seconds)

    async def stop(self) -> None:
        if self.speaker:
            await self.speaker.stop()


@dataclass
class MockAudioPort:
    responses: list[str] = field(default_factory=list)
    interrupted: bool = False
    fail_on: str | None = None
    log: list[tuple[str, Any]] = field(default_factory=list)

    def _check(self, op: str) -> None:
        if self.fail_on == op:
            raise AudioUnavailable(f"mock failure on {op}")

    async def chime(self) -> None:
        self._check("chime")
        self.log.append(("chime", None))

    async def say(self, text: str, *, length_scale: float | None = None) -> None:
        self._check("say")
        self.log.append(("say", text))

    async def listen(self, req: ListenRequest) -> ListenResult:
        self._check("listen")
        self.log.append(("listen", req))
        text = self.responses.pop(0) if self.responses else ""
        return ListenResult(text=text, speech_seconds=len(text.split()) / 2.5,
                            interrupted=self.interrupted and req.interrupt_after is not None,
                            no_speech=not text.strip())

    async def hold_music(self, max_seconds: float) -> None:
        self.log.append(("hold_music", max_seconds))
        try:
            await asyncio.sleep(max_seconds)
        except asyncio.CancelledError:
            self.log.append(("hold_music_stopped", None))
            raise

    async def stop(self) -> None:
        self.log.append(("stop", None))

    @property
    def spoken(self) -> list[str]:
        return [t for op, t in self.log if op == "say"]
