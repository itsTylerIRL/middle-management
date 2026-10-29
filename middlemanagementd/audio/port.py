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


@dataclass
class BargeIn:
    """How to react if the user talks while the manager is speaking.

    yield:     stop talking and keep recording; the user's speech is returned (as with listen).
    talk_over: keep talking, louder by `boost` (still capped), and just report that it happened.
    """

    mode: str = "yield"
    listen: ListenRequest = field(default_factory=ListenRequest)
    boost: float = 1.25

    def to_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "listen": self.listen.to_dict(), "boost": self.boost}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "BargeIn":
        listen = ListenRequest(**{k: v for k, v in (d.get("listen") or {}).items()
                                  if k in ListenRequest.__dataclass_fields__})
        return cls(mode=str(d.get("mode", "yield")), listen=listen, boost=float(d.get("boost", 1.25)))


@dataclass
class SayResult:
    barged_in: bool = False
    # Populated in yield mode: what the user said after cutting in.
    text: str = ""
    speech_seconds: float = 0.0
    interrupted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AudioPort(Protocol):
    async def chime(self) -> None: ...

    async def say(self, text: str, *, length_scale: float | None = None, barge_in: BargeIn | None = None,
                  gain: float = 1.0) -> SayResult: ...

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

    async def say(self, text: str, *, length_scale: float | None = None, barge_in: BargeIn | None = None,
                  gain: float = 1.0) -> SayResult:
        return await self.engine.say(text, length_scale=length_scale, barge_in=barge_in, gain=gain)

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

    async def say(self, text: str, *, length_scale: float | None = None, barge_in: BargeIn | None = None,
                  gain: float = 1.0) -> SayResult:
        self.print(f"[bold cyan]Manager:[/] {text}")
        if self.speaker:
            await self.speaker.say(text, length_scale=length_scale, gain=gain)
        return SayResult()

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
    # say-call index -> what the user says while barging in on that utterance
    barge_at: dict[int, str] = field(default_factory=dict)
    log: list[tuple[str, Any]] = field(default_factory=list)
    gains: list[float] = field(default_factory=list)
    _says: int = 0

    def _check(self, op: str) -> None:
        if self.fail_on == op:
            raise AudioUnavailable(f"mock failure on {op}")

    async def chime(self) -> None:
        self._check("chime")
        self.log.append(("chime", None))

    async def say(self, text: str, *, length_scale: float | None = None, barge_in: BargeIn | None = None,
                  gain: float = 1.0) -> SayResult:
        self._check("say")
        self.log.append(("say", text))
        self.gains.append(gain)
        idx, self._says = self._says, self._says + 1
        if barge_in is None or idx not in self.barge_at:
            return SayResult()
        said = self.barge_at[idx]
        self.log.append(("barge_in", (barge_in.mode, said)))
        if barge_in.mode == "yield":
            return SayResult(barged_in=True, text=said, speech_seconds=len(said.split()) / 2.5)
        return SayResult(barged_in=True)

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
