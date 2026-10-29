"""AudioEngine: everything that touches the speakers and microphone on the audio machine."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import wave
from pathlib import Path

import numpy as np

from ..config import Config, expand
from ..logs import event
from .devices import Player, ProcessRegistry, Recorder
from .port import ListenRequest, ListenResult
from .speech import STT, TTS, clean_transcript
from .synth import SR as SYNTH_SR
from .synth import hold_music_loop, synergy_chime
from .vad import VADState

log = logging.getLogger(__name__)

FRAME_MS = 30


class AudioEngine:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.registry = ProcessRegistry()
        self.player = Player(self.registry, cfg.audio.output_device)
        self.recorder = Recorder(self.registry, cfg.audio.input_device)
        self.tts = TTS(cfg.tts)
        self.stt = STT(cfg.stt)
        self._speak_lock = asyncio.Lock()

    def load(self, stt: bool = True) -> None:
        """Load models up front so the first interrogation isn't slowed by it."""
        self.tts.load()
        if stt:
            self.stt.load()

    @property
    def gain(self) -> float:
        # The cap is enforced here, on the machine with the speakers, whatever the daemon requests.
        return min(self.cfg.audio.volume, self.cfg.audio.max_volume)

    def kill_all(self) -> int:
        n = self.registry.kill_all()
        if n:
            event(log, "audio silenced", processes=n)
        return n

    async def chime(self) -> None:
        await self.player.play(synergy_chime(), SYNTH_SR, min(self.gain * self.cfg.audio.chime_gain, self.cfg.audio.max_volume))

    async def hold_music(self, max_seconds: float) -> None:
        max_seconds = min(max_seconds, self.cfg.audio.max_utterance_seconds * 2)
        await self.player.play(hold_music_loop(), SYNTH_SR, self.gain * self.cfg.audio.hold_music_gain,
                               loop_seconds=max_seconds)

    async def say(self, text: str, *, length_scale: float | None = None) -> None:
        samples, rate = await self.tts.synthesize(text, length_scale)
        samples = samples[: int(rate * self.cfg.audio.max_utterance_seconds)]
        async with self._speak_lock:
            await self.player.play(samples, rate, self.gain)

    async def listen(self, req: ListenRequest) -> ListenResult:
        frame_s = FRAME_MS / 1000
        vad = VADState(frame_seconds=frame_s, energy_threshold=self.cfg.listen.energy_threshold,
                       noise_multiplier=self.cfg.listen.noise_multiplier,
                       max_threshold=self.cfg.listen.max_threshold)
        started = time.monotonic()
        interrupted = False
        hard_limit = req.start_timeout + req.max_seconds * 1.5 + 5
        async with contextlib.aclosing(self.recorder.frames(FRAME_MS)) as frames:
            async for frame in frames:
                vad.push(frame)
                elapsed = time.monotonic() - started
                if elapsed > hard_limit:
                    break
                if not vad.in_speech:
                    if elapsed > req.start_timeout:
                        break
                    continue
                if vad.speech_seconds >= req.max_seconds:
                    break
                if req.interrupt_after is not None and vad.speech_seconds >= req.interrupt_after:
                    interrupted = True
                    break
                if vad.silence_seconds >= req.end_silence:
                    break
        event(log, "listen finished", speech_seconds=round(vad.speech_seconds, 1), interrupted=interrupted,
              noise_floor=round(vad.noise_floor, 4))
        if not vad.in_speech or vad.speech_seconds < req.min_speech:
            return ListenResult(no_speech=True)
        audio = vad.audio()
        if self.cfg.storage.keep_recordings:
            self._save_recording(audio)
        text = clean_transcript(await self.stt.transcribe(audio), vad.speech_seconds)
        event(log, "transcribed", words=len(text.split()))
        return ListenResult(text=text, speech_seconds=vad.speech_seconds, interrupted=interrupted,
                            no_speech=not text)

    def _save_recording(self, audio: np.ndarray) -> None:
        d = expand(self.cfg.storage.recordings_dir)
        d.mkdir(parents=True, exist_ok=True)
        path = Path(d) / time.strftime("status-%Y%m%d-%H%M%S.wav")
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.recorder.rate)
            w.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())
        event(log, "recording kept", path=str(path))
