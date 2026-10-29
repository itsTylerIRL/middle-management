"""Command-line interface. Talks to the daemon over its HTTP API; a few commands run locally."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import __version__
from .config import Config, load_config
from .escalation import LEVEL_NAMES
from .logs import setup_logging
from .scoring import METRICS

console = Console()
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", "~/.local/state")).expanduser() / "middlemanagementd"


# --- daemon API ---------------------------------------------------------------
class ApiError(RuntimeError):
    pass


def api(cfg: Config, method: str, path: str, body: dict[str, Any] | None = None, timeout: float = 10) -> Any:
    url = cfg.server.url.rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else (b"" if method == "POST" else None)
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    if token := os.environ.get(cfg.server.token_env):
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read())
        except ValueError:
            payload = {"error": str(e)}
        if e.code == 409:
            return payload
        raise ApiError(payload.get("error") or payload.get("message") or str(e)) from e
    except (urllib.error.URLError, OSError) as e:
        raise ApiError(f"daemon not reachable at {cfg.server.url} ({getattr(e, 'reason', e)})") from e


def daemon_up(cfg: Config) -> bool:
    try:
        api(cfg, "GET", "/api/status", timeout=2)
        return True
    except ApiError:
        return False


# --- rendering ----------------------------------------------------------------
def bar(value: float, width: int = 20) -> str:
    filled = int(round(value / 100 * width))
    color = "green" if value >= 70 else "yellow" if value >= 45 else "red"
    return f"[{color}]{'█' * filled}[/][dim]{'░' * (width - filled)}[/] {value:5.1f}"


def render_evaluation(ev: dict[str, Any], transcript: str | None = None) -> None:
    table = Table(show_header=False, box=None, padding=(0, 1))
    for m in METRICS:
        table.add_row(m.replace("_", " ").title(), bar(ev["scores"].get(m, 0)))
    verdict = "[bold green]PASS: EXECUTIVE MATERIAL[/]" if ev["passed"] else "[bold red]FAIL: INSUFFICIENT SYNERGY[/]"
    lines = [
        verdict,
        f"Overall [bold]{ev['overall']:.1f}[/] / threshold {ev['threshold']:.1f}  "
        f"[dim]({ev['evaluator']}, {ev['word_count']} words, {ev['jargon_ratio'] * 100:.0f}% jargon)[/]",
    ]
    if ev.get("detected_jargon"):
        lines.append("Detected: [magenta]" + ", ".join(ev["detected_jargon"]) + "[/]")
    if ev.get("critique"):
        lines.append(f"[italic]{ev['critique']}[/]")
    if ev.get("llm_error"):
        lines.append(f"[dim]LLM unavailable, deterministic scoring used: {ev['llm_error'][:100]}[/]")
    if transcript is not None:
        console.print(Panel(transcript or "[dim](silence)[/]", title="Your status update", border_style="blue"))
    console.print(Panel(table, title="Performance Review", border_style="magenta"))
    console.print("\n".join(lines))


def render_event(e: dict[str, Any]) -> None:
    t, d = e["type"], e["data"]
    if t == "session_started":
        console.rule(f"[bold magenta]Interrogation by {d['personality']}[/]")
    elif t == "question":
        console.print(f"[bold cyan]{d['personality']}:[/] {d['text']}")
    elif t == "transcript":
        note = " [red](interrupted)[/]" if d.get("interrupted") else ""
        console.print(Panel(d["text"] or "[dim](silence)[/]", title=f"You{note}", border_style="blue"))
    elif t == "evaluation":
        render_evaluation(d)
    elif t == "feedback":
        level = d.get("level", 0)
        console.print(Panel(d["text"], title=f"Feedback: level {level} {LEVEL_NAMES.get(level, '')}",
                            border_style="red" if level >= 3 else "yellow" if level else "green"))
    elif t == "rank_change":
        console.print(f"[bold]Title change:[/] {d['from']} → [bold green]{d['to']}[/]")
    elif t == "session_aborted":
        console.print(f"[red]Interrogation aborted:[/] {d['reason']}")
    elif t == "session_finished":
        console.print(f"[dim]Finished in {d['duration_seconds']}s. Rank: {d['rank_after']}[/]")


def render_stats(s: dict[str, Any]) -> None:
    t = Table(title="Corporate Reputation", show_header=False, title_style="bold magenta")
    t.add_row("Current rank", f"[bold]{s['rank']}[/] (#{s['rank_number']})")
    t.add_row("Synergy points", f"{s['synergy_points']}" + (
        f" [dim]({s['points_to_next_rank']} to next rank)[/]" if s["points_to_next_rank"] is not None else ""))
    t.add_row("Escalation level", f"{s['escalation_level']} ({LEVEL_NAMES.get(s['escalation_level'])})")
    t.add_row("Total interrogations", str(s["total_interrogations"]))
    t.add_row("Successful status reports", f"[green]{s['successful_reports']}[/]")
    t.add_row("Failed evaluations", f"[red]{s['failed_evaluations']}[/]")
    t.add_row("Average score", str(s["average_score"]))
    t.add_row("Average jargon density", f"{s['average_jargon_density'] * 100:.1f}%")
    t.add_row("Longest successful monologue", f"{s['longest_successful_monologue_words']} words")
    t.add_row("Consecutive successes", f"{s['consecutive_successes']} (best {s['best_streak']})")
    t.add_row("PIPs issued", str(s["pips_issued"]))
    if s.get("open_pip"):
        t.add_row("[red]Open PIP[/]", ", ".join(s["open_pip"]["terms"]))
    console.print(t)
    if s["top_buzzwords"]:
        b = Table(title="Most Leveraged Buzzwords", title_style="bold magenta")
        b.add_column("Term")
        b.add_column("Uses", justify="right")
        for term, n in s["top_buzzwords"]:
            b.add_row(term, str(n))
        console.print(b)


# --- commands -----------------------------------------------------------------
def cmd_serve(cfg: Config, args: argparse.Namespace) -> int:
    from .daemon import Daemon

    if args.host:
        cfg.server.host = args.host
    if args.port:
        cfg.server.port = args.port
    audio = None
    if args.local_audio:
        from .audio.engine import AudioEngine
        from .audio.port import LocalAudioPort

        engine = AudioEngine(cfg)
        engine.load()
        audio = LocalAudioPort(engine)

    async def run() -> None:
        await Daemon(cfg, audio=audio).serve()

    asyncio.run(run())
    return 0


def cmd_audio_client(cfg: Config, args: argparse.Namespace) -> int:
    from .audio.client import AudioClient
    from .audio.engine import AudioEngine

    engine = AudioEngine(cfg)
    console.print("[dim]Loading speech models...[/]")
    engine.load()
    client = AudioClient(cfg, engine, args.url or cfg.server.url, os.environ.get(cfg.server.token_env, ""), args.name)
    asyncio.run(client.main())
    return 0


def _spawn(argv: list[str], logname: str) -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    logf = open(STATE_DIR / logname, "ab")  # noqa: SIM115 - handed to the child
    proc = subprocess.Popen([sys.executable, "-m", "middlemanagementd", *argv], stdout=logf, stderr=logf,
                            stdin=subprocess.DEVNULL, start_new_session=True)
    return proc.pid


def cmd_start(cfg: Config, args: argparse.Namespace) -> int:
    base = ["--config", cfg.source_path] if cfg.source_path else []
    if daemon_up(cfg):
        console.print("[yellow]Daemon already running.[/]")
    else:
        pid = _spawn([*base, "serve"], "daemon.log")
        for _ in range(50):
            if daemon_up(cfg):
                break
            time.sleep(0.2)
        else:
            console.print(f"[red]Daemon did not come up; see {STATE_DIR / 'daemon.log'}[/]")
            return 1
        console.print(f"[green]middlemanagementd started[/] (pid {pid}). Synergy is now being enforced.")
    if args.with_audio:
        pid = _spawn([*base, "audio-client"], "audio-client.log")
        console.print(f"[green]Audio client started[/] (pid {pid}); log: {STATE_DIR / 'audio-client.log'}")
    return 0


def _signal_audio_client(sig: signal.Signals) -> bool:
    from .audio.client import pidfile_path

    try:
        os.kill(int(pidfile_path().read_text().strip()), sig)
        return True
    except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
        return False


def cmd_stop(cfg: Config, args: argparse.Namespace) -> int:
    try:
        api(cfg, "POST", "/api/shutdown")
        console.print("[green]Daemon stopping.[/] The meeting is over.")
    except ApiError as e:
        console.print(f"[yellow]{e}[/]")
    if args.audio and _signal_audio_client(signal.SIGTERM):
        console.print("[green]Local audio client stopped.[/]")
    return 0


def _simple(path: str):  # noqa: ANN202
    def run(cfg: Config, args: argparse.Namespace) -> int:
        res = api(cfg, "POST", path)
        console.print(("[green]" if res.get("ok") else "[red]") + res.get("message", "") + "[/]")
        return 0 if res.get("ok") else 1

    return run


def cmd_hush(cfg: Config, args: argparse.Namespace) -> int:
    local = _signal_audio_client(signal.SIGUSR1)
    console.print("[green]Local audio silenced.[/]" if local else "[dim]No local audio client running.[/]")
    try:
        api(cfg, "POST", "/api/kill", timeout=3)
        console.print("[green]Kill switch engaged on daemon.[/] Run `middlemanagementd revive` to resume.")
    except ApiError as e:
        console.print(f"[yellow]Could not reach daemon: {e}[/]")
    return 0


def cmd_status(cfg: Config, args: argparse.Namespace) -> int:
    s = api(cfg, "GET", "/api/status?check=1", timeout=15)
    t = Table(show_header=False, box=None)
    state = ("[bold red]KILL SWITCH ENGAGED[/]" if s["killswitch"] else "[yellow]paused[/]" if s["paused"]
             else "[green]enforcing synergy[/]")
    t.add_row("State", state + (" [magenta](evaluation in progress)[/]" if s["busy"] else ""))
    audio = s["audio"]
    t.add_row("Audio client", "[red]none connected[/]" if not audio else "local" if audio == "local"
              else f"{audio['name']} since {datetime.fromtimestamp(audio['connected_at']):%H:%M:%S}")
    nxt = s["next_interrogation"]
    t.add_row("Next interrogation", nxt.replace("T", " ") if nxt else "[dim]not scheduled[/]")
    t.add_row("Escalation", f"level {s['escalation_level']}: {s['escalation_name']}")
    t.add_row("Rank", s["rank"])
    llm = s.get("llm_health", {})
    t.add_row("LLM", f"{s['llm_endpoint']} " + ("[green]up[/]" if llm.get("ok") else f"[red]down[/] {llm.get('error', '')}"))
    t.add_row("Uptime", f"{s['uptime_seconds'] // 3600}h {s['uptime_seconds'] % 3600 // 60}m")
    console.print(Panel(t, title=f"middlemanagementd {s['version']}", border_style="magenta"))
    return 0


def cmd_trigger(cfg: Config, args: argparse.Namespace) -> int:
    body = {"personality": args.personality} if args.personality else {}
    res = api(cfg, "POST", "/api/trigger", body)
    if not res.get("ok"):
        console.print(f"[red]{res.get('message')}[/]")
        return 1
    console.print("[magenta]Interrogation initiated.[/] Please hold for your manager.")
    if args.no_follow:
        return 0
    since = res.get("seq", 0)
    deadline = time.time() + 600
    try:
        while time.time() < deadline:
            for e in api(cfg, "GET", f"/api/events?since={since}"):
                since = e["seq"]
                render_event(e)
                if e["type"] in ("session_finished", "session_aborted"):
                    return 0
            time.sleep(0.5)
    except KeyboardInterrupt:
        console.print("[dim]Stopped following (the interrogation continues). Use `hush` to silence it.[/]")
    return 0


def cmd_stats(cfg: Config, args: argparse.Namespace) -> int:
    if args.local:
        from .config import expand
        from .reputation import Store

        render_stats(Store(expand(cfg.storage.database_path)).stats())
    else:
        render_stats(api(cfg, "GET", "/api/stats"))
    return 0


def cmd_history(cfg: Config, args: argparse.Namespace) -> int:
    rows = api(cfg, "GET", f"/api/history?limit={args.limit}")
    t = Table(title="Interrogation History", title_style="bold magenta")
    for col in ("When", "Manager", "Score", "Result", "Level", "Transcript"):
        t.add_column(col)
    for r in rows:
        t.add_row(datetime.fromtimestamp(r["ts"]).strftime("%m-%d %H:%M"), r["personality"], f"{r['overall']:.0f}",
                  "[green]PASS[/]" if r["passed"] else "[red]FAIL[/]", f"{r['level_before']}→{r['level_after']}",
                  (r["transcript"] or "")[:60])
    console.print(t)
    return 0


def cmd_config(cfg: Config, args: argparse.Namespace) -> int:
    import yaml

    if args.init:
        dest = Path("~/.config/middlemanagementd/config.yaml").expanduser()
        if dest.exists():
            console.print(f"[yellow]{dest} already exists; not overwriting.[/]")
            return 1
        src = Path(__file__).resolve().parent.parent / "config.example.yaml"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(src.read_text() if src.exists() else yaml.safe_dump(cfg.to_dict(), sort_keys=False))
        console.print(f"[green]Wrote {dest}[/]")
        return 0
    console.print(f"[dim]Config file: {cfg.source_path or 'none (built-in defaults)'}[/]")
    console.print(yaml.safe_dump(cfg.to_dict(), sort_keys=False))
    return 0


def cmd_test_audio(cfg: Config, args: argparse.Namespace) -> int:
    from .audio.devices import list_devices
    from .audio.engine import AudioEngine
    from .audio.port import AudioError, ListenRequest

    console.print(f"Output devices: {list_devices('sinks') or '[red]none / pactl unavailable[/]'}")
    console.print(f"Input devices:  {[d for d in list_devices('sources') if not d.endswith('.monitor')]}")
    console.print(f"Configured: out={cfg.audio.output_device} in={cfg.audio.input_device} "
                  f"volume={cfg.audio.volume} (cap {cfg.audio.max_volume})")
    which = set(args.only or ["chime", "tts", "mic"])
    engine = AudioEngine(cfg)

    async def run() -> int:
        failures = 0
        steps = [
            ("chime", "Synergy Chime", engine.chime),
            ("hold", "Hold music (4s)", lambda: engine.hold_music(4)),
            ("tts", "Text-to-speech", lambda: engine.say(
                "This is a test of the corporate alignment broadcast system. Please align accordingly.")),
        ]
        for key, label, fn in steps:
            if key not in which:
                continue
            console.print(f"[cyan]▶ {label}[/]")
            if key == "tts":
                engine.tts.load()
                console.print(f"  [dim]TTS backend: {engine.tts.backend}[/]")
            try:
                await fn()
                console.print("  [green]ok[/]")
            except AudioError as e:
                failures += 1
                console.print(f"  [red]failed: {e}[/]")
        if "mic" in which:
            console.print("[cyan]▶ Microphone + speech recognition[/] Say something corporate (8s)...")
            try:
                engine.stt.load()
                res = await engine.listen(ListenRequest(max_seconds=8, start_timeout=6, end_silence=1.5))
                console.print(f"  heard: [bold]{res.text or '(nothing)'}[/] [dim]({res.speech_seconds:.1f}s speech)[/]")
            except AudioError as e:
                failures += 1
                console.print(f"  [red]failed: {e}[/]")
        return 1 if failures else 0

    return asyncio.run(run())


def cmd_evaluate(cfg: Config, args: argparse.Namespace) -> int:
    from .evaluator import JargonEvaluator
    from .llm import LLMClient
    from .personalities import pick_personality

    import random

    if args.no_llm:
        cfg.llm.enabled = False
    persona = pick_personality(args.personality or "passive_aggressive", cfg.personality.enabled, random.Random())
    text = " ".join(args.text) if args.text else sys.stdin.read()

    async def run() -> int:
        llm = LLMClient(cfg.llm)
        try:
            ev = await JargonEvaluator(cfg.evaluation, llm, cfg.llm.max_tokens_eval, cfg.llm.eval_temperature).evaluate(
                "Quick pulse check. Where are we on the current deliverables?", text, persona)
        finally:
            await llm.close()
        if args.json:
            print(json.dumps(ev.to_dict(), indent=2))
        else:
            console.print(f"[dim]Graded by: {persona.title}[/]")
            render_evaluation(ev.to_dict(), text)
        return 0

    return asyncio.run(run())


def cmd_rehearse(cfg: Config, args: argparse.Namespace) -> int:
    """Full interrogation in this terminal: typed answers, optional real audio output."""
    from .audio.port import ConsoleAudioPort, LocalAudioPort
    from .config import expand
    from .evaluator import JargonEvaluator
    from .llm import LLMClient
    from .reputation import Store
    from .session import InterrogationRunner

    if args.no_llm:
        cfg.llm.enabled = False
    cfg.audio.hold_music = cfg.audio.hold_music and args.speak
    speaker = None
    if args.speak:
        from .audio.engine import AudioEngine

        engine = AudioEngine(cfg)
        engine.load(stt=False)
        speaker = LocalAudioPort(engine)
    store = Store(expand(cfg.storage.database_path) if args.record else ":memory:")
    if args.level is not None:
        store.level = args.level

    async def run() -> int:
        llm = LLMClient(cfg.llm)
        evaluator = JargonEvaluator(cfg.evaluation, llm, cfg.llm.max_tokens_eval, cfg.llm.eval_temperature)
        runner = InterrogationRunner(cfg, store, llm, evaluator, emit=lambda k, d: render_event(
            {"type": k, "data": d}) if k in ("evaluation", "rank_change") else None)
        try:
            for _ in range(args.rounds):
                out = await runner.run(ConsoleAudioPort(speaker, console.print), trigger="rehearsal",
                                       personality=args.personality)
                console.print(f"[dim]Level {out.level_before}→{out.level_after}, rank {out.rank_after}[/]\n")
        finally:
            await llm.close()
        return 0

    try:
        return asyncio.run(run())
    except (KeyboardInterrupt, EOFError):
        console.print("\n[dim]Meeting adjourned.[/]")
        return 0


def cmd_download_models(cfg: Config, args: argparse.Namespace) -> int:
    from .config import expand

    voice_dir = expand(cfg.tts.voice_dir)
    voice_dir.mkdir(parents=True, exist_ok=True)
    console.print(f"Downloading Piper voice [bold]{cfg.tts.voice}[/] to {voice_dir}")
    rc = subprocess.call([sys.executable, "-m", "piper.download_voices", "--data-dir", str(voice_dir), cfg.tts.voice])
    console.print(f"Fetching Whisper model [bold]{cfg.stt.model}[/] (Hugging Face cache)")
    try:
        from faster_whisper import WhisperModel

        WhisperModel(cfg.stt.model, device=cfg.stt.device, compute_type=cfg.stt.compute_type)
        console.print("[green]Whisper model ready.[/]")
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]Whisper download failed: {e}[/]")
        rc = rc or 1
    return rc


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="middlemanagementd", description="Corporate Alignment Enforcement Daemon")
    p.add_argument("--config", "-c", help="path to config.yaml")
    p.add_argument("--verbose", "-v", action="store_true")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("serve", help="run the daemon in the foreground (systemd uses this)")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.add_argument("--local-audio", action="store_true", help="drive this machine's audio in-process")
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("audio-client", help="run the audio client (on the machine with mic/speakers)")
    s.add_argument("--url", help="daemon URL (default: server.url)")
    s.add_argument("--name", help="client name (default: hostname)")
    s.set_defaults(func=cmd_audio_client)

    s = sub.add_parser("start", help="start the daemon in the background")
    s.add_argument("--with-audio", action="store_true", help="also start a local audio client")
    s.set_defaults(func=cmd_start)
    s = sub.add_parser("stop", help="stop the daemon")
    s.add_argument("--audio", action="store_true", help="also stop the local audio client")
    s.set_defaults(func=cmd_stop)

    for name, path, helptext in (("pause", "/api/pause", "suspend scheduled interrogations"),
                                 ("resume", "/api/resume", "resume scheduled interrogations"),
                                 ("kill", "/api/kill", "KILL SWITCH: silence everything, stop scheduling"),
                                 ("revive", "/api/revive", "release the kill switch")):
        sub.add_parser(name, help=helptext).set_defaults(func=_simple(path))
    sub.add_parser("hush", help="silence the local audio client immediately (works without the daemon)"
                   ).set_defaults(func=cmd_hush)

    sub.add_parser("status", help="daemon status").set_defaults(func=cmd_status)
    s = sub.add_parser("trigger", help="interrogate me now")
    s.add_argument("--personality", "-p")
    s.add_argument("--no-follow", action="store_true", help="don't stream the session to the terminal")
    s.set_defaults(func=cmd_trigger)
    s = sub.add_parser("stats", help="corporate reputation")
    s.add_argument("--local", action="store_true", help="read the database directly instead of via the daemon")
    s.set_defaults(func=cmd_stats)
    s = sub.add_parser("history", help="recent interrogations")
    s.add_argument("--limit", "-n", type=int, default=15)
    s.set_defaults(func=cmd_history)
    s = sub.add_parser("config", help="show effective config")
    s.add_argument("--init", action="store_true", help="write ~/.config/middlemanagementd/config.yaml")
    s.set_defaults(func=cmd_config)
    s = sub.add_parser("test-audio", help="test speakers, TTS, microphone and STT on this machine")
    s.add_argument("--only", nargs="+", choices=["chime", "hold", "tts", "mic"])
    s.set_defaults(func=cmd_test_audio)
    s = sub.add_parser("evaluate", help="grade text without audio")
    s.add_argument("text", nargs="*")
    s.add_argument("--personality", "-p")
    s.add_argument("--no-llm", action="store_true")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_evaluate)
    s = sub.add_parser("rehearse", help="practice in the terminal: typed answers, no daemon needed")
    s.add_argument("--personality", "-p")
    s.add_argument("--speak", action="store_true", help="speak through this machine's speakers")
    s.add_argument("--no-llm", action="store_true")
    s.add_argument("--record", action="store_true", help="count toward your real reputation")
    s.add_argument("--level", type=int, choices=range(6), help="start at this escalation level")
    s.add_argument("--rounds", type=int, default=1)
    s.set_defaults(func=cmd_rehearse)
    sub.add_parser("download-models", help="fetch the Piper voice and Whisper model").set_defaults(
        func=cmd_download_models)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
    except (OSError, ValueError) as e:
        console.print(f"[red]config error: {e}[/]")
        return 2
    setup_logging(cfg.logging, args.verbose)
    try:
        return int(args.func(cfg, args) or 0)
    except ApiError as e:
        console.print(f"[red]{e}[/]")
        if "not reachable" in str(e):
            console.print("[dim]Start it with `middlemanagementd start` or `systemctl --user start middlemanagementd`.[/]")
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
