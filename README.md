# middlemanagementd

**Corporate Alignment Enforcement Daemon.** At unpredictable intervals it plays an obnoxiously upbeat
notification chime and demands a verbal status report. A local LLM grades how convincingly *corporate* your
answer sounds: jargon density, buzzword diversity, executive confidence, and your ability to avoid answering
the question. Fail, and an increasingly insufferable middle manager pushes back, then cuts you off, then
melts down into a buzzword monologue, then puts you on a Performance Improvement Plan. Succeed, and you are
praised for your executive leadership potential despite having said nothing.

Everything runs locally: llama.cpp for judgement, faster-whisper for ears, Piper for a voice. No cloud, no
paid APIs, and no recordings kept.

## Quick start (single machine)

```bash
git clone <this repo> ~/middle-management && cd ~/middle-management
python3 -m venv .venv
.venv/bin/pip install -e '.[audio,dev]'
.venv/bin/middlemanagementd download-models        # Piper voice (~120 MB) + Whisper small.en (~250 MB)
.venv/bin/middlemanagementd config --init          # writes ~/.config/middlemanagementd/config.yaml
.venv/bin/middlemanagementd test-audio             # chime, TTS, microphone + transcription

.venv/bin/middlemanagementd start --with-audio     # daemon + local audio client, in the background
.venv/bin/middlemanagementd trigger                # interrogate me now
```

No microphone handy? `middlemanagementd rehearse` runs a full interrogation in the terminal with typed
answers (`--speak` to hear the manager anyway). `middlemanagementd evaluate "your text"` just grades text.

## Commands

| Command | |
|---|---|
| `serve [--local-audio]` | Run the daemon in the foreground (systemd uses this) |
| `audio-client [--url URL]` | Run the audio client on the machine with mic/speakers |
| `start [--with-audio]` / `stop [--audio]` | Background start/stop without systemd |
| `pause` / `resume` | Suspend/resume *scheduled* interrogations |
| `trigger [-p PERSONALITY] [--no-follow]` | Interrogate now and stream the session to the terminal |
| **`kill`** / `revive` | **Kill switch**: silence everything, halt scheduling, refuse triggers; persists across restarts |
| **`hush`** | Silence the local audio client *immediately* (works even if the daemon is unreachable), then `kill` |
| **`tui`** | **Full-screen management console** (see below) |
| `status` | State, audio client, next interrogation, escalation level, rank, LLM health |
| `stats` / `history` | Corporate reputation and recent interrogations |
| `config [--init]` | Show the effective config / write a starter config |
| `test-audio [--only chime hold tts mic barge]` | Device check on this machine; `barge` tunes talk-over detection |
| `evaluate TEXT [--no-llm] [--json]` | Grade text without audio |
| `rehearse [--speak] [--level N] [-p P]` | Interactive text session; in-memory unless `--record` |
| `download-models` | Fetch the configured Piper voice and Whisper model |

Personalities: `passive_aggressive`, `big_four`, `disappointed_vp`, `scrum_master`, `linkedin`,
`chief_synergy`. Each has its own questions, vocabulary, interruption lines, speech rate, grading priorities
and pass threshold.

## Management console (TUI)

`middlemanagementd tui` opens a live dashboard. It is a plain client of the daemon's HTTP API, so it works
from any machine that can reach the daemon.

* **Status**: state, audio client, countdown to the next interrogation, LLM health, selected manager
* **Escalation ladder**: levels 0–5 with your current level marked
* **Corporate reputation**: rank, progress to the next title, streaks, PIPs, times you talked over management
* **Most leveraged buzzwords**
* **Live interrogation feed**: question, your transcribed answer, barge-ins, the manager's feedback, as they happen
* **Last performance review**: per-metric score bars and verdict
* **History** of past interrogations

| Key | |
|---|---|
| `t` | Interrogate me now (with the selected manager) |
| `m` | Cycle manager: random → each personality |
| `p` | Pause / resume scheduling |
| `k` | **Kill switch** |
| `r` | Revive |
| `h` | Hush: silence the local audio client instantly, plus kill switch |
| `s` | Start the daemon + local audio client (when offline) |
| `q` | Quit (the daemon keeps running) |

## Split deployment: daemon on deepdish, audio on chef

The daemon needs no audio hardware or audio dependencies. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

**On deepdish** (daemon):

```bash
cd ~/middle-management && python3 -m venv .venv && .venv/bin/pip install -e .
mkdir -p ~/.config/middlemanagementd
cp config.example.yaml ~/.config/middlemanagementd/config.yaml
#   server.host: <deepdish tailnet IP or 0.0.0.0>
#   llm.base_url: http://pepper.local:8000/v1   (or pepper's tailnet IP)
echo "MMD_TOKEN=$(openssl rand -hex 24)" > ~/.config/middlemanagementd/env && chmod 600 ~/.config/middlemanagementd/env
cp systemd/middlemanagementd.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now middlemanagementd
loginctl enable-linger "$USER"    # keep user services running without a login session
```

**On chef** (audio client):

```bash
.venv/bin/pip install -e '.[audio]' && .venv/bin/middlemanagementd download-models
#   config: server.url: http://<deepdish>:8742
#   ~/.config/middlemanagementd/env: the same MMD_TOKEN
cp systemd/middlemanagementd-audio.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now middlemanagementd-audio
```

Then from either machine (with `server.url` and `MMD_TOKEN` set): `middlemanagementd status`.

## Configuration

See [`config.example.yaml`](config.example.yaml); every option is commented. Config is looked up at
`--config`, `$MMD_CONFIG`, `~/.config/middlemanagementd/config.yaml`, then `./config.yaml`. Secrets
are never read from YAML: the API token and the optional LLM key come from environment variables.

Main options: LLM endpoint/model/timeouts, STT model/device, TTS voice, input/output devices, volume and
its hard cap, interval range and `frequency`, quiet hours and weekdays, `difficulty`, personality,
`aggressiveness`, interruption thresholds, speech timeout and grace period, scoring weights, and database path.

## Scoring

Each answer gets eight 0–100 scores. A deterministic scorer (`scoring.py` + the
[jargon dictionary](middlemanagementd/data/jargon.yaml)) always runs. When the LLM is reachable its scores
are blended in (`llm_weight`, default 0.7). Code, not the LLM, computes the weighted overall and the
pass/fail, using your weights, the personality's priorities and the difficulty threshold. If pepper is down,
grading continues on rules alone and the manager falls back to fixed lines. Prompts and real outputs are
in [docs/evaluation-prompts.md](docs/evaluation-prompts.md).

Concrete information (numbers, "fixed", "merged", "yesterday", "because") *lowers* your evasion score.
This is by design.

## Escalation

| Level | | Behaviour |
|---|---|---|
| 0 | Good Standing | |
| 1 | Gentle Pushback | Coaching tone |
| 2 | Alignment Concerns | Mildly ominous |
| 3 | Executive Intervention | You get cut off after 30 s of talking |
| 4 | Corporate Meltdown | Cut off after 12 s; streamed buzzword monologue (time-capped) |
| 5 | Performance Improvement Plan | Formal notice + 5 required terms; next check-in must use ≥ 4 of them and pass |

Failures add `aggressiveness` levels, and passes remove `decay_on_pass`. Passing the PIP drops you to level 2.

Ranks run from *Unaligned Individual Contributor* to *Global Thought Leadership Evangelist*, driven by synergy
points (pass: +score/8, fail: −2, PIP: −15).

## Talking over the manager

You can cut in while the manager is speaking:

| When | What happens |
|---|---|
| During the question | The manager stops and your speech becomes the answer (eager, but allowed) |
| Feedback at levels 0–2 | The manager yields. If your interjection is corporate enough, it concedes ("let's take the rest offline") and your escalation level drops by one. Otherwise: "Let's put a pin in that. As I was saying…" |
| Feedback at level 3+ | The manager refuses to yield: "I'm still talking." Each interruption makes it louder (still capped by `max_volume`) |

`stats` counts the times you talked over management.

**How it hears you over itself:** the client knows what it is playing, so it predicts the energy of its own echo at
the mic, using an echo gain and delay calibrated from the chime and refined after every uninterrupted sentence.
You count as talking over it when the mic is clearly louder than that prediction for about 0.35 s.

* **Headphones**: calibration measures no echo, so this is just a voice detector and works well.
* **Speakers**: you must be clearly louder than the manager *at the microphone*. It helps to keep the speaker
  volume sane, use a mic close to you, and keep other audio off. Run `middlemanagementd test-audio --only barge`:
  it shows how close your voice gets to the threshold and suggests a `barge_in_ratio`.
* `echo_cancel: pipewire` loads PipeWire's WebRTC echo canceller at runtime, nothing persisted. On chef's
  HDMI output it was inconsistent in testing, so it's off by default; try it if your hardware cooperates.

## Hold music

While the LLM deliberates, the client plays synthesized elevator jazz. To use your own track (*Opus No. 1*,
say), point `audio.hold_music_file` at a copy you have. Any format ffmpeg reads works. It restarts from the
top on every hold, as is traditional.

## Safety and privacy

* **It can always be silenced**: `hush` (local, instant), `kill` (global, persistent),
  `systemctl --user stop middlemanagementd-audio`, or simply losing the network (the client goes quiet when
  it loses the daemon). Every sound is a killable subprocess, and every playback has a duration cap.
* Volume is capped client-side at `max_volume`, relative to your system volume. If your sink is at 160%,
  the cap is too, so check `pactl get-sink-volume @DEFAULT_SINK@`.
* Microphone audio stays in memory and is discarded after transcription. Transcripts (text) are stored in
  SQLite unless `store_transcripts: false`.
* No root needed. The units are user services with `NoNewPrivileges`.
* The API binds to localhost by default. Before binding elsewhere, set `MMD_TOKEN`.

## Development

```bash
.venv/bin/python -m pytest      # 76 tests; mock audio + fake LLM, no devices or network needed
```

The tests cover deterministic scoring, LLM parsing and fallback, the scheduler (quiet hours, weekdays,
frequency), escalation and PIPs, VAD, reputation/ranks, the whole interrogation slice with mock adapters, and
the daemon and audio client over a real WebSocket, including the kill switch and local hush.

## Known limitations

* Over loud speakers you have to out-shout the manager at the mic for barge-in to register (see above).
* The energy VAD is simple. In a loud room, raise `listen.energy_threshold` or use a headset.
* With a ~27B model at ~7 tok/s, grading takes 20–35 s (hence the hold music). A smaller model makes the
  manager quicker but less eloquent.
