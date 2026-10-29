# Architecture

```
          deepdish (or anywhere)                                 chef (has mic + speakers)
 ┌─────────────────────────────────────────┐        ┌────────────────────────────────────────┐
 │ middlemanagementd serve                 │        │ middlemanagementd audio-client          │
 │                                         │  WS    │                                        │
 │  Scheduler ──► Daemon ──► Interrogation │◄──────►│  AudioClient ──► AudioEngine           │
 │   (quiet hrs,    │         Runner       │ /ws/   │   (ops, cancel,    ├─ Player  (paplay)  │
 │    pause, kill)  │           │          │ audio  │    stop, hush)     ├─ Recorder(parecord)│
 │                  │           ├─ Evaluator ──┐   │        │           ├─ VAD (energy)      │
 │  HTTP /api/* ◄── CLI         ├─ Escalation  │   │        │           ├─ TTS (Piper/espeak)│
 │                  │           └─ Store (SQLite) │        │           └─ STT (faster-whisper)│
 └──────────────────┼──────────────────────────┼───┘        └────────────────────────────────────────┘
                    │                          │ HTTP (OpenAI-compatible)
                    │                          ▼
                    │                 pepper: llama.cpp server :8000 (Qwen3.8-27B)
```

## Why split the daemon from the audio?

The daemon host (`deepdish`) has no microphone or speakers. Everything that touches sound lives in the
**audio client** on the machine you sit at (`chef`). The daemon sends it high-level ops and gets text
back, so only text crosses the network: no audio streaming and no codec. STT and TTS run where the
audio is, on chef's CPU (faster-whisper `small.en` int8 transcribes in about 1 s; Piper is faster than real time).

For a single machine, `middlemanagementd serve --local-audio` runs the same engine in-process.

## Components

| Module | Role |
|---|---|
| `config.py` | Typed dataclass config, YAML overlay, validation |
| `scheduler.py` | Pure timing logic: random intervals / frequency, quiet hours (wrap midnight), weekdays |
| `daemon.py` | aiohttp app: control API, `/ws/audio` hub, scheduler loop, kill switch, lifecycle |
| `session.py` | `InterrogationRunner`: one interrogation from chime to SQLite row |
| `personalities.py` | Six managers: question banks, vocabulary, interrupt lines, scoring biases, speech rate |
| `scoring.py` + `data/jargon.yaml` | Deterministic jargon scoring (always computed) |
| `evaluator.py` | LLM scoring blended with rules (`llm_weight`); falls back to rules alone |
| `prompts.py` | Evaluator and character prompts (see `docs/evaluation-prompts.md`) |
| `escalation.py` | Levels 0–5, interruption policy, PIP rules, fallback lines |
| `reputation.py` | SQLite: history, buzzword counts, PIPs, persisted state (level, pause, kill switch), ranks |
| `llm.py` | OpenAI-compatible client: timeouts, retries with backoff, SSE streaming, sentence splitter |
| `audio/port.py` | `AudioPort` protocol + Local / Console / Mock adapters |
| `audio/engine.py` | The real thing: chime, hold music, say, listen (VAD → STT) |
| `audio/client.py` | WebSocket client that executes ops; local hush via SIGUSR1 |
| `cli.py` | Rich terminal UI over the HTTP API |

## Interrogation flow

1. Chime (synthesised: there are no audio assets).
2. Question from the chosen personality. If a PIP is open, it is the PIP check-in instead.
3. `listen`: VAD waits up to `grace_period_seconds` for speech and ends after `end_silence_seconds`.
   At escalation level ≥ 3 it also takes an `interrupt_after` limit: once you've talked that long, the client
   stops recording and the manager cuts in ("I'm going to stop you right there").
4. A filler line ("Let me run that through our proprietary maturity framework"), then **hold music** while
   the evaluator runs (the LLM on pepper generates about 7 tok/s, so grading takes 20–35 s).
5. Escalation decides the stage: praise, levels 1–5, or PIP completed/failed.
6. Character feedback is **streamed**: the LLM response is split into sentences and each one is spoken as
   soon as it is complete, while later ones are still generating. The level-4 meltdown is capped at
   `monologue_max_seconds`.
7. The result is persisted, and a rank change is announced.

## Silencing (the daemon must never become impossible to silence)

| Mechanism | Scope |
|---|---|
| `middlemanagementd kill` | Persisted kill switch: client gets `stop`, the session task is cancelled, the scheduler halts, and triggers are refused until `revive`. Survives restarts. |
| `middlemanagementd hush` | SIGUSR1 to the local audio client (works with the daemon down), plus `kill` if reachable |
| Audio client disconnect | The client kills all audio when it loses the daemon |
| `systemctl --user stop middlemanagementd-audio` | SIGTERM kills all audio subprocesses |
| Ctrl-C on `trigger` | Only stops following; the session continues (use `hush`) |
| Hard caps | `max_volume` enforced client-side; `max_utterance_seconds` per playback; `monologue_max_seconds`; listen wall-clock limit |

Every sound is a `paplay` subprocess fed in 250 ms chunks, so cancelling means SIGKILL, and silence
follows within one audio buffer.

## Extending

* **Web dashboard**: everything the CLI shows comes from `/api/status`, `/api/stats`, `/api/history`
  and `/api/events?since=N` (an event stream for a live view). A dashboard is just another API client.
* **Barge-in / true full-duplex**: currently the manager talks over *you* (record → cut off → speak).
  Detecting you talking over *it* needs echo cancellation (PipeWire `libpipewire-module-echo-cancel`)
  so the mic doesn't hear the speakers. Add it as a `say(..., listen=True)` op once AEC is set up.
* **Silero VAD**: `audio/vad.py` is energy-based. faster-whisper bundles a Silero model if the room is noisy.
