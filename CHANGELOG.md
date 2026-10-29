# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-10-29

First public release.

### Added

- **Daemon** (`serve`): randomised interrogation schedule with quiet hours, active weekdays, pause/resume, manual
  trigger, and a persistent kill switch. HTTP control API with optional bearer token.
- **Audio client** (`audio-client`), split from the daemon so the service can run on a machine without speakers.
  Only text crosses the network. Synthesised chime and elevator hold music (or your own file via ffmpeg),
  Piper/espeak TTS, faster-whisper STT, energy VAD, and a client-side volume cap. `hush` silences it locally.
- **Grading**: eight corporate metrics from a deterministic jargon scorer blended with a local LLM; pass/fail
  decided by code from configurable weights and difficulty.
- **Escalation**: Gentle Pushback, Alignment Concerns, Executive Intervention (you get cut off), Corporate Meltdown
  (streamed monologue), and Performance Improvement Plan, with required terms for the next check-in.
- **Six managers** with their own questions, vocabulary, speech rate and grading priorities.
- **Full duplex**: talk over the manager. It yields at low escalation (and concedes to a sufficiently corporate
  interjection), and gets louder at high escalation. Echo-aware barge-in detection, tunable with
  `test-audio --only barge`.
- **Weekly performance review**, read aloud on Friday afternoons or filed silently, with a rating decided by code
  and a write-up by the LLM (or an HR template).
- **Corporate reputation**: SQLite history, ranks from *Unaligned Individual Contributor* to *Global Thought
  Leadership Evangelist*, streaks, buzzword leaderboard.
- **Management console** (`tui`): live dashboard, interrogation history with full detail, and reviews.
- CLI: `status`, `trigger`, `stats`, `history`, `review`, `rehearse` (typed practice), `evaluate`, `test-audio`,
  `download-models`, `config --init`; systemd user units.

[0.1.0]: https://github.com/itsTylerIRL/middle-management/releases/tag/v0.1.0
