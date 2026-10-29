"""Text-to-speech (Piper, espeak-ng fallback) and speech recognition (faster-whisper)."""

from __future__ import annotations

import asyncio
import io
import logging
import shutil
import subprocess
import wave
from typing import Any

import numpy as np

from ..config import STTConfig, TTSConfig, expand
from ..logs import event
from .port import AudioUnavailable

log = logging.getLogger(__name__)


def _wav_to_float(data: bytes) -> tuple[np.ndarray, int]:
    with wave.open(io.BytesIO(data)) as w:
        rate, width, channels = w.getframerate(), w.getsampwidth(), w.getnchannels()
        raw = w.readframes(w.getnframes())
    if width != 2:
        raise ValueError(f"unsupported sample width {width}")
    samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    return samples, rate


class TTS:
    def __init__(self, cfg: TTSConfig) -> None:
        self.cfg = cfg
        self._voice: Any = None
        self.backend = cfg.backend

    def load(self) -> None:
        if self.backend != "piper":
            return
        model = expand(self.cfg.voice_dir) / f"{self.cfg.voice}.onnx"
        try:
            from piper import PiperVoice  # type: ignore[import-not-found]

            if not model.is_file():
                raise FileNotFoundError(f"{model} (run: middlemanagementd download-models)")
            self._voice = PiperVoice.load(str(model))
            event(log, "piper voice loaded", voice=self.cfg.voice)
        except Exception as e:  # noqa: BLE001 - any failure here means "use the fallback"
            fallback = "espeak" if shutil.which("espeak-ng") else "none"
            event(log, "piper unavailable, falling back", logging.WARNING, error=repr(e)[:200], fallback=fallback)
            self.backend = fallback

    def _synth_piper(self, text: str, length_scale: float) -> tuple[np.ndarray, int]:
        from piper import SynthesisConfig  # type: ignore[import-not-found]

        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            self._voice.synthesize_wav(text, w, syn_config=SynthesisConfig(length_scale=length_scale))
        return _wav_to_float(buf.getvalue())

    def _synth_espeak(self, text: str, length_scale: float) -> tuple[np.ndarray, int]:
        wpm = int(175 / max(0.5, length_scale))
        out = subprocess.run(["espeak-ng", "-v", self.cfg.espeak_voice, "-s", str(wpm), "--stdout", text],
                             capture_output=True, timeout=60, check=True).stdout
        return _wav_to_float(out)

    async def synthesize(self, text: str, length_scale: float | None = None) -> tuple[np.ndarray, int]:
        ls = (length_scale or 1.0) * self.cfg.length_scale
        if self.backend == "piper" and self._voice is not None:
            return await asyncio.to_thread(self._synth_piper, text, ls)
        if self.backend == "espeak":
            return await asyncio.to_thread(self._synth_espeak, text, ls)
        raise AudioUnavailable("no TTS backend available")


class STT:
    def __init__(self, cfg: STTConfig) -> None:
        self.cfg = cfg
        self._model: Any = None

    def load(self) -> None:
        if self.cfg.backend != "faster-whisper":
            return
        try:
            from faster_whisper import WhisperModel  # type: ignore[import-not-found]
        except ImportError as e:
            raise AudioUnavailable("faster-whisper not installed (pip install 'middlemanagementd[audio]')") from e
        self._model = WhisperModel(self.cfg.model, device=self.cfg.device, compute_type=self.cfg.compute_type)
        event(log, "whisper model loaded", model=self.cfg.model, device=self.cfg.device)

    def _transcribe(self, audio: np.ndarray) -> str:
        segments, _ = self._model.transcribe(
            audio, language="en", beam_size=self.cfg.beam_size, initial_prompt=self.cfg.initial_prompt,
            vad_filter=False, condition_on_previous_text=False,
        )
        return " ".join(s.text.strip() for s in segments).strip()

    async def transcribe(self, audio: np.ndarray) -> str:
        if self._model is None:
            raise AudioUnavailable("speech recognition unavailable")
        if len(audio) < 1600:
            return ""
        return await asyncio.to_thread(self._transcribe, audio.astype(np.float32))


# Whisper's favourite things to hear in silence.
HALLUCINATIONS = {"thank you.", "thanks for watching!", "thank you for watching.", "you", "bye.", "."}


def clean_transcript(text: str, speech_seconds: float) -> str:
    if speech_seconds < 1.5 and text.strip().lower() in HALLUCINATIONS:
        return ""
    return text.strip()
