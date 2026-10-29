"""AudioEngine: everything that touches the speakers and microphone on the audio machine."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import subprocess
import time
import wave
from collections import deque
from collections.abc import AsyncIterator
from pathlib import Path

import numpy as np

from ..config import Config, expand
from ..logs import event
from .devices import EchoCancelModule, GainControl, Player, ProcessRegistry, Recorder, check_device
from .echo import FRAME_SECONDS, BargeInDetector, EchoModel, calibrate, envelope
from .port import AudioError, AudioSilenced, BargeIn, ListenRequest, ListenResult, SayResult
from .speech import STT, TTS, clean_transcript
from .synth import SR as SYNTH_SR
from .synth import hold_music_loop, synergy_chime
from .vad import VADState

log = logging.getLogger(__name__)

FRAME_MS = int(FRAME_SECONDS * 1000)
HOLD_MUSIC_MAX_SECONDS = 900
# Keep listening this long after playback so the echo tail is measured (and still guarded).
POST_ROLL_FRAMES = 15


def _rms(frame: np.ndarray) -> float:
    return float(np.sqrt(np.mean(frame * frame))) if len(frame) else 0.0


def _dense(heard: dict[int, float]) -> np.ndarray:
    env = np.zeros(max(heard) + 1 if heard else 0, dtype=np.float32)
    last = 0.0
    for i in range(len(env)):
        last = heard.get(i, last)
        env[i] = last
    return env


def decode_audio_file(path: Path, rate: int = SYNTH_SR, max_seconds: float = HOLD_MUSIC_MAX_SECONDS) -> np.ndarray:
    """Decode any format ffmpeg understands to float32 mono, peak-normalised."""
    out = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-t", str(max_seconds),
         "-f", "f32le", "-ac", "1", "-ar", str(rate), "-"],
        capture_output=True, timeout=120, check=True,
    ).stdout
    samples = np.frombuffer(out, dtype="<f4").copy()
    if not len(samples):
        raise ValueError(f"{path}: no audio decoded")
    return samples / max(float(np.abs(samples).max()), 1e-6) * 0.8


class AudioEngine:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.registry = ProcessRegistry()
        self.player = Player(self.registry, cfg.audio.output_device)
        self.recorder = Recorder(self.registry, cfg.audio.input_device)
        self.tts = TTS(cfg.tts)
        self.stt = STT(cfg.stt)
        self.echo = EchoModel()
        self.noise_floor: float | None = None
        self.echo_cancel: EchoCancelModule | None = None
        self._hold_music: np.ndarray | None = None
        self._speak_lock = asyncio.Lock()
        # Diagnostics from the most recent barge-in-monitored utterance (for `test-audio --only barge`).
        self.last_barge_ratio = 0.0

    def load(self, stt: bool = True) -> None:
        """Load models and set up devices up front so the first interrogation isn't slowed by it."""
        self.tts.load()
        if stt:
            self.stt.load()
        if self.cfg.audio.echo_cancel == "pipewire":
            self.echo_cancel = EchoCancelModule(self.cfg.audio.output_device, self.cfg.audio.input_device)
            try:
                sink, source = self.echo_cancel.load()
                self.player.device, self.recorder.device = sink, source
            except AudioError as e:
                event(log, "echo cancellation unavailable; using the raw devices", logging.WARNING, error=str(e))
                self.echo_cancel = None

    def close(self) -> None:
        self.kill_all()
        if self.echo_cancel is not None:
            self.echo_cancel.unload()
            self.echo_cancel = None

    @property
    def gain(self) -> float:
        # The cap is enforced here, on the machine with the speakers, whatever the daemon requests.
        return min(self.cfg.audio.volume, self.cfg.audio.max_volume)

    def _capped(self, gain: float) -> float:
        return max(0.0, min(gain, self.cfg.audio.max_volume))

    def kill_all(self) -> int:
        n = self.registry.kill_all()
        if n:
            event(log, "audio silenced", processes=n)
        return n

    # --- playback ------------------------------------------------------------
    def hold_music_samples(self) -> np.ndarray:
        if self._hold_music is None:
            path = self.cfg.audio.hold_music_file
            if path:
                try:
                    self._hold_music = decode_audio_file(expand(path))
                    event(log, "hold music loaded", path=path, seconds=round(len(self._hold_music) / SYNTH_SR))
                except (OSError, ValueError, subprocess.SubprocessError) as e:
                    event(log, "hold music file unusable; using the synthesized loop", logging.WARNING,
                          path=path, error=str(e)[:200])
            if self._hold_music is None:
                self._hold_music = hold_music_loop()
        return self._hold_music

    async def hold_music(self, max_seconds: float) -> None:
        max_seconds = min(max_seconds, self.cfg.audio.max_utterance_seconds * 2)
        await self.player.play(self.hold_music_samples(), SYNTH_SR,
                               self._capped(self.gain * self.cfg.audio.hold_music_gain), loop_seconds=max_seconds)

    async def chime(self) -> None:
        samples, gain = synergy_chime(), self._capped(self.gain * self.cfg.audio.chime_gain)
        if not self.cfg.audio.barge_in:
            await self.player.play(samples, SYNTH_SR, gain)
            return
        try:
            check_device(self.recorder.device, "sources")
        except AudioError:
            await self.player.play(samples, SYNTH_SR, gain)
            return
        # The chime doubles as a first echo measurement (refined later from speech).
        ref = envelope(samples * gain, SYNTH_SR)
        heard: dict[int, float] = {}
        async with self._monitor(samples, SYNTH_SR, GainControl(gain)) as (play_task, frames):
            async for idx, frame in frames:
                if idx >= 0:
                    heard[idx] = _rms(frame)
                if play_task.done() and idx > len(ref) + POST_ROLL_FRAMES:
                    break
        mic_env = _dense(heard)
        if len(mic_env) > 12:
            self.noise_floor = float(np.median(mic_env[-10:]))
            self._learn_echo(ref, mic_env, "chime")

    def _learn_echo(self, ref: np.ndarray, mic_env: np.ndarray, source: str) -> None:
        noise = self.noise_floor if self.noise_floor is not None else float(np.percentile(mic_env, 10))
        model = calibrate(ref, mic_env, noise)
        if model is None or model.quality < 0.5:
            event(log, "echo fit rejected", logging.DEBUG, source=source,
                  quality=None if model is None else round(model.quality, 2))
            return
        if self.echo.calibrated:
            # Smooth the gain across utterances; trust the newest delay.
            model.gain = 0.5 * self.echo.gain + 0.5 * model.gain
        self.echo = model
        event(log, "echo calibrated", source=source, gain=round(model.gain, 3),
              delay_ms=model.delay_frames * FRAME_MS, quality=round(model.quality, 2))

    async def say(self, text: str, *, length_scale: float | None = None, barge_in: BargeIn | None = None,
                  gain: float = 1.0) -> SayResult:
        samples, rate = await self.tts.synthesize(text, length_scale)
        samples = samples[: int(rate * self.cfg.audio.max_utterance_seconds)]
        ctl = GainControl(self._capped(self.gain * gain))
        async with self._speak_lock:
            if barge_in is None or not self.cfg.audio.barge_in:
                await self.player.play(samples, rate, ctl)
                return SayResult()
            return await self._say_with_barge_in(samples, rate, ctl, barge_in)

    async def _say_with_barge_in(self, samples: np.ndarray, rate: int, ctl: GainControl,
                                 barge_in: BargeIn) -> SayResult:
        a = self.cfg.audio
        noise = self.noise_floor if self.noise_floor is not None else self.cfg.listen.energy_threshold
        # Reference envelope at the level actually played; the echo model was fitted the same way.
        ref = envelope(samples * ctl.value, rate)
        detector = BargeInDetector(ref, self.echo, noise, ratio=a.barge_in_ratio, min_seconds=a.barge_in_min_seconds)
        preroll: deque[np.ndarray] = deque(maxlen=25)
        heard: dict[int, float] = {}
        vad: VADState | None = None
        barged_at = 0.0
        interrupted = False
        req = barge_in.listen
        async with self._monitor(samples, rate, ctl) as (play_task, frames):
            async for idx, frame in frames:
                if vad is None:
                    rms = _rms(frame)
                    preroll.append(frame)
                    if idx >= 0:
                        heard[idx] = rms
                    if idx >= 0 and detector.push(idx, rms):
                        event(log, "barge-in detected", mode=barge_in.mode, echo_calibrated=self.echo.calibrated)
                        if barge_in.mode != "yield":
                            ctl.value = self._capped(ctl.value * barge_in.boost)
                            await asyncio.wait({play_task})
                            return SayResult(barged_in=True)
                        play_task.cancel()
                        vad = self._new_vad(noise)
                        vad.in_speech, vad.frames, vad.speech_frames = True, list(preroll), detector.need
                        barged_at = time.monotonic()
                    elif play_task.done() and idx > len(ref) + POST_ROLL_FRAMES:
                        break
                    continue
                vad.push(frame)
                done, interrupted = self._listen_done(vad, req, time.monotonic() - barged_at, waiting=False)
                if done:
                    break
        self.last_barge_ratio = detector.sustained_ratio
        if vad is None:
            # Uninterrupted speech is the best echo measurement there is.
            self._learn_echo(ref, _dense(heard), "speech")
            return SayResult()
        res = await self._finish_listen(vad, req, interrupted)
        return SayResult(barged_in=True, text=res.text, speech_seconds=res.speech_seconds,
                         interrupted=res.interrupted)

    @contextlib.asynccontextmanager
    async def _monitor(self, samples: np.ndarray, rate: int,
                       ctl: GainControl) -> AsyncIterator[tuple[asyncio.Task[None], AsyncIterator[tuple[int, np.ndarray]]]]:
        """Play while recording. Yields (play_task, frames) where frames gives (playback frame index, mic frame).

        Mic frames are counted rather than timestamped (they arrive in bursts; the sample clock doesn't
        jitter). The index is -1 until audio has been handed to the player; constant offsets (buffering,
        device latency) are absorbed by the echo delay calibration, which uses this same path.
        """
        count = 0
        start_frame: list[int] = []
        play_task = asyncio.create_task(
            self.player.play(samples, rate, ctl, on_start=lambda: start_frame.append(count)))

        async def frames() -> AsyncIterator[tuple[int, np.ndarray]]:
            nonlocal count
            async with contextlib.aclosing(self.recorder.frames(FRAME_MS)) as mic:
                async for frame in mic:
                    count += 1
                    yield (count - start_frame[0] if start_frame else -1), frame

        gen = frames()
        try:
            yield play_task, gen
        finally:
            await gen.aclose()
            if not play_task.done():
                play_task.cancel()
            try:
                await play_task
            except (asyncio.CancelledError, AudioSilenced):
                # Cancelled because the user barged in, or the op itself was cancelled.
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise

    # --- capture ---------------------------------------------------------------
    def _new_vad(self, noise_floor: float | None = None) -> VADState:
        lc = self.cfg.listen
        vad = VADState(frame_seconds=FRAME_SECONDS, energy_threshold=lc.energy_threshold,
                       noise_multiplier=lc.noise_multiplier, max_threshold=lc.max_threshold)
        if noise_floor is not None:
            vad.noise_floor, vad.calibration_frames = noise_floor, 0
        return vad

    @staticmethod
    def _listen_done(vad: VADState, req: ListenRequest, elapsed: float, waiting: bool) -> tuple[bool, bool]:
        """(finished, interrupted) for a listen in progress."""
        if elapsed > req.start_timeout + req.max_seconds * 1.5 + 5:
            return True, False
        if not vad.in_speech:
            return waiting and elapsed > req.start_timeout, False
        if vad.speech_seconds >= req.max_seconds:
            return True, False
        if req.interrupt_after is not None and vad.speech_seconds >= req.interrupt_after:
            return True, True
        return vad.silence_seconds >= req.end_silence, False

    async def listen(self, req: ListenRequest) -> ListenResult:
        vad = self._new_vad()
        started = time.monotonic()
        interrupted = False
        async with contextlib.aclosing(self.recorder.frames(FRAME_MS)) as frames:
            async for frame in frames:
                vad.push(frame)
                done, interrupted = self._listen_done(vad, req, time.monotonic() - started, waiting=True)
                if done:
                    break
        return await self._finish_listen(vad, req, interrupted)

    async def _finish_listen(self, vad: VADState, req: ListenRequest, interrupted: bool) -> ListenResult:
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
