"""Configuration: typed dataclasses with sensible defaults, overlaid by YAML."""

from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from datetime import time as dtime
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

CONFIG_ENV = "MMD_CONFIG"
DEFAULT_CONFIG_PATHS = (
    Path("~/.config/middlemanagementd/config.yaml"),
    Path("./config.yaml"),
)


@dataclass
class LLMConfig:
    enabled: bool = True
    # Any OpenAI-compatible endpoint; this is llama.cpp's llama-server on its default port.
    base_url: str = "http://127.0.0.1:8080/v1"
    model: str = "local"
    # Name of an env var holding an API key, if the endpoint needs one. Never put keys in YAML.
    api_key_env: str = "MMD_LLM_API_KEY"
    connect_timeout_seconds: float = 5.0
    timeout_seconds: float = 90.0
    max_retries: int = 2
    retry_backoff_seconds: float = 1.5
    temperature: float = 0.9
    eval_temperature: float = 0.2
    max_tokens_eval: int = 220
    max_tokens_feedback: int = 140
    max_tokens_monologue: int = 380
    max_tokens_pip: int = 220
    max_tokens_review: int = 520
    max_tokens_coaching: int = 320
    max_tokens_word: int = 120
    # Qwen3-style templates: skip <think> blocks, which are slow and unspoken.
    disable_thinking: bool = True
    json_mode: bool = True


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8742
    # Where the CLI and audio client find the daemon.
    url: str = "http://127.0.0.1:8742"
    # Env var holding a shared bearer token. Unset = no auth (fine on localhost).
    token_env: str = "MMD_TOKEN"


@dataclass
class SchedulerConfig:
    enabled: bool = True
    min_interval_minutes: float = 45.0
    max_interval_minutes: float = 150.0
    # >1 means more frequent interrogations, <1 less frequent.
    frequency: float = 1.0
    quiet_hours: list[str] = field(default_factory=lambda: ["21:30-08:30"])
    # Monday=0 ... Sunday=6. Management respects weekends. Mostly.
    active_weekdays: list[int] = field(default_factory=lambda: [0, 1, 2, 3, 4])
    start_paused: bool = False


@dataclass
class AudioConfig:
    output_device: str = "default"
    input_device: str = "default"
    volume: float = 0.6
    # Hard ceiling enforced by the audio client regardless of what the daemon asks for.
    max_volume: float = 0.85
    chime_gain: float = 1.0
    hold_music: bool = True
    # Your own audio file (any format ffmpeg reads) to loop instead of the synthesized elevator jazz.
    hold_music_file: str = ""
    hold_music_gain: float = 0.45
    # No single playback may exceed this, so nothing can drone on forever.
    max_utterance_seconds: float = 90.0
    # Let the user talk over the manager. Detection is echo-aware (calibrated from the chime).
    barge_in: bool = True
    # Mic energy must exceed predicted echo + noise by this factor...
    barge_in_ratio: float = 1.5
    # ...for this long before it counts as talking over the manager.
    barge_in_min_seconds: float = 0.35
    # "none" (raw devices; fine with headphones) or "pipewire" (load module-echo-cancel at runtime; Linux with
    # the pulse backend only).
    echo_cancel: str = "none"
    # "auto" (paplay/parecord on Linux when installed, PortAudio elsewhere), "pulse" or "portaudio".
    # Device names: PulseAudio names for pulse; for portaudio, any part of a name from `test-audio`.
    backend: str = "auto"


@dataclass
class ListenConfig:
    # Maximum length of your status report.
    speech_timeout_seconds: float = 60.0
    # How long you have to start talking after the question.
    grace_period_seconds: float = 8.0
    end_silence_seconds: float = 1.8
    min_speech_seconds: float = 0.4
    # Absolute RMS floor (0..1) for voice activity; adaptive noise estimate is used above it.
    energy_threshold: float = 0.010
    noise_multiplier: float = 3.0
    max_threshold: float = 0.15


@dataclass
class TTSConfig:
    # piper | system (macOS `say`, Windows SAPI) | espeak | none. If Piper can't load, the first of
    # system / espeak that this machine has is used instead.
    backend: str = "piper"
    voice_dir: str = "~/.local/share/middlemanagementd/voices"
    voice: str = "en_US-ryan-high"
    length_scale: float = 1.0
    espeak_voice: str = "en-us"


@dataclass
class STTConfig:
    backend: str = "faster-whisper"  # faster-whisper | none
    model: str = "small.en"
    device: str = "cpu"
    compute_type: str = "int8"
    beam_size: int = 1
    # Biases the recogniser towards spelling buzzwords correctly.
    initial_prompt: str = (
        "Status update: synergy, leverage, stakeholders, north star, OKRs, KPIs, deliverables, "
        "bandwidth, circle back, cross-functional alignment, value-add, roadmap."
    )


@dataclass
class EvaluationConfig:
    difficulty: str = "normal"
    pass_thresholds: dict[str, float] = field(
        default_factory=lambda: {"intern": 45.0, "normal": 58.0, "director": 68.0, "executive": 78.0}
    )
    # Blend between LLM scores and deterministic scores (1.0 = LLM only).
    llm_weight: float = 0.7
    weights: dict[str, float] = field(
        default_factory=lambda: {
            "jargon_density": 1.5,
            "buzzword_diversity": 1.0,
            "executive_confidence": 1.0,
            "strategic_alignment": 1.0,
            "stakeholder_awareness": 0.8,
            "actionability_theater": 0.8,
            "corporate_verbosity": 0.7,
            "question_evasion": 1.2,
        }
    )
    # Optional path to a custom jargon dictionary (defaults to the bundled one).
    jargon_dictionary: str = ""


@dataclass
class EscalationConfig:
    # Levels gained per failure. Fractions are applied probabilistically.
    aggressiveness: float = 1.0
    decay_on_pass: int = 2
    interrupt_enabled: bool = True
    # Seconds of your speech the manager tolerates before cutting you off, by current level.
    interrupt_after_seconds: dict[int, float] = field(default_factory=lambda: {3: 30.0, 4: 12.0, 5: 20.0})
    monologue_max_seconds: float = 75.0
    # From this level up, the manager refuses to yield when talked over and gets louder instead.
    talk_over_level: int = 3
    talk_over_boost: float = 1.25
    pip_required_terms: int = 5
    pip_terms_to_pass: int = 4
    use_llm: bool = True


@dataclass
class PersonalityConfig:
    # "random" or one of: passive_aggressive, big_four, disappointed_vp, scrum_master,
    # linkedin, chief_synergy
    default: str = "random"
    enabled: list[str] = field(
        default_factory=lambda: [
            "passive_aggressive",
            "big_four",
            "disappointed_vp",
            "scrum_master",
            "linkedin",
            "chief_synergy",
        ]
    )


@dataclass
class ReviewConfig:
    # The weekly performance review: a formal summary of your week, read aloud by a manager.
    enabled: bool = True
    weekday: int = 4  # Monday=0 ... Friday=4
    time: str = "16:55"  # the traditional moment for bad news
    personality: str = "disappointed_vp"  # or "random"
    # Read it aloud when allowed (not paused/killed, audio connected, outside quiet hours);
    # otherwise it is filed silently for the TUI and `middlemanagementd review`.
    deliver_aloud: bool = True


@dataclass
class PracticeConfig:
    # After each interrogation: your answer rewritten as an executive would say it, and why it works.
    coaching: bool = True
    # One term a day, favouring ones you rarely use. Working it into an answer earns bonus points, once a day.
    word_of_day: bool = True
    word_of_day_points: float = 5.0


@dataclass
class StorageConfig:
    database_path: str = "~/.local/share/middlemanagementd/middlemanagementd.db"
    store_transcripts: bool = True
    # Raw audio is never written unless this is explicitly enabled.
    keep_recordings: bool = False
    recordings_dir: str = "~/.local/share/middlemanagementd/recordings"


@dataclass
class LoggingConfig:
    level: str = "INFO"
    format: str = "text"  # text | json
    file: str = ""


@dataclass
class Config:
    llm: LLMConfig = field(default_factory=LLMConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    listen: ListenConfig = field(default_factory=ListenConfig)
    tts: TTSConfig = field(default_factory=TTSConfig)
    stt: STTConfig = field(default_factory=STTConfig)
    evaluation: EvaluationConfig = field(default_factory=EvaluationConfig)
    escalation: EscalationConfig = field(default_factory=EscalationConfig)
    personality: PersonalityConfig = field(default_factory=PersonalityConfig)
    review: ReviewConfig = field(default_factory=ReviewConfig)
    practice: PracticeConfig = field(default_factory=PracticeConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    source_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("source_path", None)
        return d


def _overlay(target: Any, data: dict[str, Any], path: str = "") -> None:
    known = {f.name: f for f in fields(target)}
    for key, value in data.items():
        if key not in known:
            log.warning("unknown config key %s%s ignored", path, key)
            continue
        current = getattr(target, key)
        if is_dataclass(current):
            if not isinstance(value, dict):
                raise ValueError(f"config section {path}{key} must be a mapping")
            _overlay(current, value, f"{path}{key}.")
        elif isinstance(current, dict) and isinstance(value, dict):
            merged = dict(current)
            merged.update(value)
            # YAML keys like `3:` arrive as ints already; keep them as-is.
            setattr(target, key, merged)
        else:
            setattr(target, key, value)


def validate(cfg: Config) -> None:
    s = cfg.scheduler
    if s.min_interval_minutes <= 0 or s.max_interval_minutes < s.min_interval_minutes:
        raise ValueError("scheduler: need 0 < min_interval_minutes <= max_interval_minutes")
    if s.frequency <= 0:
        raise ValueError("scheduler.frequency must be > 0")
    if not 0 < cfg.audio.max_volume <= 1.0:
        raise ValueError("audio.max_volume must be in (0, 1]")
    if cfg.audio.echo_cancel not in ("none", "pipewire"):
        raise ValueError("audio.echo_cancel must be 'none' or 'pipewire'")
    if cfg.audio.backend not in ("auto", "pulse", "portaudio"):
        raise ValueError("audio.backend must be 'auto', 'pulse' or 'portaudio'")
    if cfg.tts.backend not in ("piper", "system", "espeak", "none"):
        raise ValueError("tts.backend must be 'piper', 'system', 'espeak' or 'none'")
    if cfg.evaluation.difficulty not in cfg.evaluation.pass_thresholds:
        raise ValueError(f"evaluation.difficulty must be one of {sorted(cfg.evaluation.pass_thresholds)}")
    if not 0.0 <= cfg.evaluation.llm_weight <= 1.0:
        raise ValueError("evaluation.llm_weight must be within [0, 1]")
    if not 0 <= cfg.review.weekday <= 6:
        raise ValueError("review.weekday must be 0 (Monday) .. 6 (Sunday)")
    try:
        dtime.fromisoformat(cfg.review.time)
    except ValueError as e:
        raise ValueError(f"review.time must be HH:MM: {e}") from e
    cfg.escalation.interrupt_after_seconds = {
        int(k): float(v) for k, v in cfg.escalation.interrupt_after_seconds.items()
    }


def find_config_path(explicit: str | None = None) -> Path | None:
    if explicit:
        return Path(explicit).expanduser()
    if env := os.environ.get(CONFIG_ENV):
        return Path(env).expanduser()
    for candidate in DEFAULT_CONFIG_PATHS:
        p = candidate.expanduser()
        if p.is_file():
            return p
    return None


def load_config(path: str | None = None) -> Config:
    cfg = Config()
    p = find_config_path(path)
    if p is not None:
        if not p.is_file():
            raise FileNotFoundError(f"config file not found: {p}")
        data = yaml.safe_load(p.read_text()) or {}
        if not isinstance(data, dict):
            raise ValueError(f"{p}: top level must be a mapping")
        _overlay(cfg, data)
        cfg.source_path = str(p)
    validate(cfg)
    return cfg


def expand(path: str) -> Path:
    return Path(os.path.expandvars(path)).expanduser()
