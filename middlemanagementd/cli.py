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
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from . import __version__, render
from .config import Config, load_config
from .logs import setup_logging

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
            if e.code in (404, 405):
                # Our handlers answer in JSON; a bare 404/405 is a route this daemon doesn't have.
                payload["error"] = (f"the daemon doesn't support {method} {path.split('?')[0]}; it is probably "
                                    "older than this client. Restart it to pick up the new version.")
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
def render_evaluation(ev: dict[str, Any], transcript: str | None = None) -> None:
    console.print(render.evaluation(ev, transcript))


def render_event(e: dict[str, Any]) -> None:
    if (r := render.event(e)) is not None:
        console.print(r)


def render_stats(s: dict[str, Any]) -> None:
    for table in render.stats_tables(s):
        console.print(table)


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


def start_background(cfg: Config, with_audio: bool) -> list[str]:
    """Start the daemon (and optionally a local audio client) detached. Returns human-readable messages."""
    base = ["--config", cfg.source_path] if cfg.source_path else []
    messages = []
    if daemon_up(cfg):
        messages.append("[yellow]Daemon already running.[/]")
    else:
        pid = _spawn([*base, "serve"], "daemon.log")
        for _ in range(50):
            if daemon_up(cfg):
                break
            time.sleep(0.2)
        else:
            raise ApiError(f"daemon did not come up; see {STATE_DIR / 'daemon.log'}")
        messages.append(f"[green]middlemanagementd started[/] (pid {pid}). Synergy is now being enforced.")
    if with_audio:
        if audio_client_running():
            messages.append("[yellow]Audio client already running.[/]")
        else:
            pid = _spawn([*base, "audio-client"], "audio-client.log")
            messages.append(f"[green]Audio client started[/] (pid {pid}); log: {STATE_DIR / 'audio-client.log'}")
    return messages


def cmd_start(cfg: Config, args: argparse.Namespace) -> int:
    for msg in start_background(cfg, args.with_audio):
        console.print(msg)
    return 0


def signal_audio_client(sig: signal.Signals | int) -> bool:
    """Signal the local audio client via its pidfile (signal 0 just checks it's alive)."""
    from .audio.client import pidfile_path

    try:
        os.kill(int(pidfile_path().read_text().strip()), sig)
        return True
    except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
        return False


def audio_client_running() -> bool:
    return signal_audio_client(0)


def cmd_stop(cfg: Config, args: argparse.Namespace) -> int:
    try:
        api(cfg, "POST", "/api/shutdown")
        console.print("[green]Daemon stopping.[/] The meeting is over.")
    except ApiError as e:
        console.print(f"[yellow]{e}[/]")
    if args.audio and signal_audio_client(signal.SIGTERM):
        console.print("[green]Local audio client stopped.[/]")
    return 0


def _simple(path: str):  # noqa: ANN202
    def run(cfg: Config, args: argparse.Namespace) -> int:
        res = api(cfg, "POST", path)
        console.print(("[green]" if res.get("ok") else "[red]") + res.get("message", "") + "[/]")
        return 0 if res.get("ok") else 1

    return run


def cmd_hush(cfg: Config, args: argparse.Namespace) -> int:
    local = signal_audio_client(signal.SIGUSR1)
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
    if rv := s.get("next_review"):
        t.add_row("Next performance review", rv.replace("T", " "))
    t.add_row("Escalation", f"level {s['escalation_level']}: {s['escalation_name']}")
    t.add_row("Rank", s["rank"])
    if w := s.get("word_of_day"):
        t.add_row("Word of the day", f"[bold green]{w['term']}[/]" + (" [green]✓[/]" if w.get("claimed_by") else ""))
    llm = s.get("llm_health", {})
    t.add_row("LLM", f"{s['llm_endpoint']} " + ("[green]up[/]" if llm.get("ok") else f"[red]down[/] {llm.get('error', '')}"))
    t.add_row("Uptime", f"{s['uptime_seconds'] // 3600}h {s['uptime_seconds'] % 3600 // 60}m")
    console.print(Panel(t, title=f"middlemanagementd {s['version']}", border_style="magenta"))
    return 0


def cmd_word(cfg: Config, args: argparse.Namespace) -> int:
    w = api(cfg, "GET", "/api/status").get("word_of_day")
    console.print(Panel(render.word_of_day(w), title="Word of the Day", border_style="green", expand=False))
    if w and not w.get("claimed_by"):
        console.print(f"[dim]Work it into an interrogation for +{cfg.practice.word_of_day_points:g} synergy points.[/]")
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


def _delete(cfg: Config, path: str) -> bool:
    res = api(cfg, "DELETE", path)
    ok = bool(res.get("ok"))
    console.print(f"[green]{res['message'].capitalize()}.[/]" if ok else f"[red]{res.get('message')}[/]")
    return ok


def cmd_history(cfg: Config, args: argparse.Namespace) -> int:
    if args.delete:
        return 0 if all([_delete(cfg, f"/api/history/{i}") for i in args.delete]) else 1
    rows = api(cfg, "GET", f"/api/history?limit={args.limit}")
    t = Table(title="Interrogation History", title_style="bold magenta")
    for col in ("#", "When", "Manager", "Score", "Result", "Level", "Transcript"):
        t.add_column(col)
    for r in rows:
        t.add_row(str(r["id"]), datetime.fromtimestamp(r["ts"]).strftime("%m-%d %H:%M"), r["personality"], f"{r['overall']:.0f}",
                  "[green]PASS[/]" if r["passed"] else "[red]FAIL[/]", f"{r['level_before']}→{r['level_after']}",
                  (r["transcript"] or "")[:60])
    console.print(t)
    return 0


def cmd_reset(cfg: Config, args: argparse.Namespace) -> int:
    if not args.yes:
        console.print("[bold red]This permanently deletes every interrogation, review and PIP,[/] and resets your "
                      "rank, streaks and escalation level.")
        if input("Type 'reset' to confirm: ").strip().lower() != "reset":
            console.print("[dim]Nothing deleted.[/]")
            return 1
    res = api(cfg, "POST", "/api/reset")
    if not res.get("ok"):
        console.print(f"[red]{res.get('message')}[/]")
        return 1
    console.print("[green]History wiped.[/] You are once again an Unaligned Individual Contributor.")
    return 0


def cmd_review(cfg: Config, args: argparse.Namespace) -> int:
    if args.delete is not None:
        return 0 if _delete(cfg, f"/api/reviews/{args.delete}") else 1
    if args.now:
        body: dict[str, Any] = {"aloud": not args.silent, "previous": args.last_week}
        if args.personality:
            body["personality"] = args.personality
        res = api(cfg, "POST", "/api/review", body)
        if not res.get("ok"):
            console.print(f"[red]{res.get('message')}[/]")
            return 1
        console.print(f"[magenta]{res['message'].capitalize()}.[/] HR is preparing your file.")
        since, deadline, review_id = res.get("seq", 0), time.time() + 900, None
        try:
            while review_id is None and time.time() < deadline:
                for e in api(cfg, "GET", f"/api/events?since={since}"):
                    since = e["seq"]
                    if e["type"] == "review_ready":
                        review_id = e["data"]["id"]
                    elif e["type"] == "session_aborted":
                        render_event(e)
                        return 1
                time.sleep(0.5)
        except KeyboardInterrupt:
            console.print("[dim]Stopped waiting (the review continues). Use `hush` to silence it.[/]")
            return 0
        if review_id is None:
            console.print("[red]Timed out waiting for the review.[/]")
            return 1
        console.print(render.review(api(cfg, "GET", f"/api/reviews/{review_id}")))
        return 0
    if args.id is not None:
        console.print(render.review(api(cfg, "GET", f"/api/reviews/{args.id}")))
        return 0
    rows = api(cfg, "GET", f"/api/reviews?limit={args.limit if args.list else 1}")
    if not rows:
        console.print("[dim]No reviews on file yet. `middlemanagementd review --now` requests one.[/]")
        return 0
    if not args.list:
        console.print(render.review(rows[0]))
        return 0
    t = Table(title="Performance Reviews", title_style="bold magenta")
    for col in ("#", "Week of", "Reviewer", "Rating", "Headline"):
        t.add_column(col)
    for r in rows:
        t.add_row(str(r["id"]), r["summary"]["week_of"], render.manager_title(r["personality"]),
                  f"[{render.rating_style(r['rating'])}]{render.stars(r['rating'])}[/] {r['rating_name']}",
                  r["review"]["headline"][:70])
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
    from .audio.port import AudioError, BargeIn, ListenRequest

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
        if "barge" in which:
            console.print("[cyan]▶ Talking over management[/] Calibrating echo with the chime...")
            try:
                engine.tts.load()
                engine.stt.load()
                await engine.chime()
                m = engine.echo
                console.print(f"  echo model: gain {m.gain:.2f}, delay {m.delay_frames * 30} ms, "
                              f"{'calibrated' if m.calibrated else '[yellow]defaults (calibration rejected)[/]'}; "
                              f"noise floor {engine.noise_floor or 0:.3f}")
                monologue = ("Let me be crystal clear about the opportunity space here. When we talk about strategic "
                             "alignment, we are talking about aligning the alignment itself, holistically, at scale, "
                             "across every swim lane in the ecosystem.")
                await engine.say(monologue, barge_in=BargeIn(mode="talk_over"))
                quiet = engine.last_barge_ratio
                console.print(f"  manager alone: reached {quiet:.2f} of threshold "
                              + ("[green](no false trigger)[/]" if quiet < 1 else "[yellow](noisy; raise barge_in_ratio)[/]"))
                console.print("  [bold]Now talk over me, loudly, as soon as I start.[/]")
                await asyncio.sleep(1.0)
                res = await engine.say(monologue, barge_in=BargeIn(mode="yield", listen=ListenRequest(
                    max_seconds=10, start_timeout=2, end_silence=1.2)))
                peak = engine.last_barge_ratio
                if res.barged_in:
                    console.print(f"  [green]Detected you talking over management[/] ({peak:.1f}x threshold). "
                                  f"Heard: [bold]{res.text or '(inaudible)'}[/]")
                else:
                    failures += 1
                    console.print(f"  [red]Not detected[/]: your loudest moment reached {peak:.2f} of the threshold. "
                                  f"Try headphones, lower speaker volume, or audio.barge_in_ratio "
                                  f"≈ {max(1.2, cfg.audio.barge_in_ratio * peak * 0.9):.1f}.")
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
    from .practice import define_word, word_of_day
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
    if not args.record and expand(cfg.storage.database_path).exists():
        # Practise the real word of the day (earning it here is only pretend).
        real = Store(expand(cfg.storage.database_path))
        if isinstance(w := real.get("word_of_day"), dict):
            store.set("word_of_day", {**w, "claimed_by": None})
        real.close()
    if args.level is not None:
        store.level = args.level

    async def run() -> int:
        llm = LLMClient(cfg.llm)
        evaluator = JargonEvaluator(cfg.evaluation, llm, cfg.llm.max_tokens_eval, cfg.llm.eval_temperature)
        runner = InterrogationRunner(cfg, store, llm, evaluator, emit=lambda k, d: render_event(
            {"type": k, "data": d}) if k in ("evaluation", "rank_change", "word_of_day", "coaching") else None)
        try:
            if cfg.practice.word_of_day and (w := word_of_day(store, evaluator.dictionary)):
                if not w["definition"] and llm.enabled:
                    await define_word(store, llm, cfg.llm.max_tokens_word)
                console.print(Panel(render.word_of_day(store.get("word_of_day")), title="Word of the Day",
                                    border_style="green", expand=False))
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


def _wait(condition, seconds: float) -> bool:  # noqa: ANN001
    deadline = time.time() + seconds
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.2)
    return False


def cmd_update(cfg: Config, args: argparse.Namespace) -> int:
    from . import update

    repo = update.checkout()
    if repo is None:
        console.print("This install isn't a git checkout. Update it with:\n"
                      f"  {sys.executable} -m pip install --upgrade 'git+{update.REPO_URL}'")
        return 1
    try:
        commits = update.pending(repo)
    except update.UpdateError as e:
        console.print(f"[red]{e}[/]")
        return 1
    if not commits:
        console.print(f"[green]Already up to date[/] ({update.git(repo, 'rev-parse', '--short', 'HEAD')}).")
        return 0
    console.print(f"[bold]{len(commits)} new commit{'s' if len(commits) != 1 else ''}:[/]")
    for line in commits:
        sha, _, subject = line.partition(" ")
        console.print(f"  [dim]{sha}[/] {escape(subject)}")
    if args.check:
        console.print("[dim]Run `middlemanagementd update` to install.[/]")
        return 0

    # What's running here, and how, so the same things come back on the new code.
    local = urllib.parse.urlsplit(cfg.server.url).hostname in ("127.0.0.1", "localhost", "::1")
    restart = not args.no_restart
    units = [u for u in (update.DAEMON_UNIT, update.AUDIO_UNIT) if restart and update.unit_active(u)]
    status = None
    try:
        status = api(cfg, "GET", "/api/status", timeout=3)
    except ApiError:
        pass
    if restart and status and status["busy"]:
        console.print("[yellow]An interrogation is in progress; run this again after it (or use --no-restart).[/]")
        return 1
    manual_daemon = restart and local and status is not None and update.DAEMON_UNIT not in units
    manual_audio = restart and update.AUDIO_UNIT not in units and audio_client_running()

    try:
        old, new, changed = update.pull(repo)
        if "pyproject.toml" in changed or args.reinstall:
            console.print("Dependencies changed; reinstalling...")
            update.reinstall(repo)
        console.print(f"[green]Updated[/] {old} → {new}.")
        if units:
            update.restart_units(units)
            console.print(f"Restarted {', '.join(units)}.")
    except update.UpdateError as e:
        console.print(f"[red]{e}[/]")
        return 1

    if manual_daemon:
        api(cfg, "POST", "/api/shutdown")
        if not _wait(lambda: not daemon_up(cfg), 20):
            console.print("[red]The old daemon didn't stop; restart it by hand.[/]")
            return 1
    if manual_audio:
        signal_audio_client(signal.SIGTERM)
        _wait(lambda: not audio_client_running(), 10)
    # _spawn runs a fresh interpreter, so these come back on the updated code.
    try:
        if manual_daemon:
            for msg in start_background(cfg, with_audio=False):
                console.print(msg)
    except ApiError as e:
        console.print(f"[red]{e}[/]")
        return 1
    if manual_audio:
        pid = _spawn([*(["--config", cfg.source_path] if cfg.source_path else []), "audio-client"],
                     "audio-client.log")
        console.print(f"[green]Audio client restarted[/] (pid {pid}).")
    if status is not None and not local:
        console.print(f"[yellow]The daemon at {cfg.server.url} runs on another machine; update it there too.[/]")
    elif not restart and (status is not None or audio_client_running()):
        console.print("[dim]Restart the daemon and audio client to use the new version.[/]")
    console.print("[dim]Restart any open consoles (`middlemanagementd tui`) as well.[/]")
    return 0


def cmd_tui(cfg: Config, args: argparse.Namespace) -> int:
    from .tui import run

    return run(cfg)


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
    sub.add_parser("tui", help="full-screen management console").set_defaults(func=cmd_tui)
    sub.add_parser("word", help="today's word of the day").set_defaults(func=cmd_word)
    s = sub.add_parser("trigger", help="interrogate me now")
    s.add_argument("--personality", "-p")
    s.add_argument("--no-follow", action="store_true", help="don't stream the session to the terminal")
    s.set_defaults(func=cmd_trigger)
    s = sub.add_parser("stats", help="corporate reputation")
    s.add_argument("--local", action="store_true", help="read the database directly instead of via the daemon")
    s.set_defaults(func=cmd_stats)
    s = sub.add_parser("history", help="recent interrogations")
    s.add_argument("--limit", "-n", type=int, default=15)
    s.add_argument("--delete", type=int, nargs="+", metavar="ID", help="delete interrogations by # (see the list)")
    s.set_defaults(func=cmd_history)
    s = sub.add_parser("review", help="weekly performance review: show the latest, list them, or request one")
    s.add_argument("--now", action="store_true", help="hold a review now (this week so far)")
    s.add_argument("--last-week", action="store_true", help="with --now: review last week instead")
    s.add_argument("--silent", action="store_true", help="with --now: file it without reading it aloud")
    s.add_argument("--personality", "-p")
    s.add_argument("--list", action="store_true", help="list past reviews")
    s.add_argument("--id", type=int, help="show one review")
    s.add_argument("--delete", type=int, metavar="ID", help="delete a review by # (see --list)")
    s.add_argument("--limit", "-n", type=int, default=15)
    s.set_defaults(func=cmd_review)
    s = sub.add_parser("reset", help="delete all history, reviews and PIPs and start over")
    s.add_argument("--yes", "-y", action="store_true", help="don't ask for confirmation")
    s.set_defaults(func=cmd_reset)
    s = sub.add_parser("update", help="pull the latest version from git, reinstall if needed, and restart")
    s.add_argument("--check", action="store_true", help="only show what's new")
    s.add_argument("--no-restart", action="store_true", help="don't restart the daemon or audio client")
    s.add_argument("--reinstall", action="store_true", help="reinstall dependencies even if they didn't change")
    s.set_defaults(func=cmd_update)
    s = sub.add_parser("config", help="show effective config")
    s.add_argument("--init", action="store_true", help="write ~/.config/middlemanagementd/config.yaml")
    s.set_defaults(func=cmd_config)
    s = sub.add_parser("test-audio", help="test speakers, TTS, microphone and STT on this machine")
    s.add_argument("--only", nargs="+", choices=["chime", "hold", "tts", "mic", "barge"])
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
