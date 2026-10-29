"""Terminal dashboard (Textual). A pure client of the daemon's HTTP API, like the CLI."""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Callable
from datetime import datetime
from typing import Any

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Footer, Header, RichLog, Static

from . import render
from .config import Config
from .escalation import LEVEL_NAMES
from .personalities import PERSONALITIES
from .reputation import RANKS

# (method, path, body) -> decoded JSON. Raises ApiError when the daemon is unreachable or refuses.
ApiCall = Callable[[str, str, dict[str, Any] | None], Any]

MANAGERS: list[str | None] = [None, *PERSONALITIES]
FEED_BACKLOG = 40


def _ago(target: datetime) -> str:
    secs = int((target - datetime.now()).total_seconds())
    if secs <= 0:
        return "imminent"
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"in {h}h {m:02d}m" if h else f"in {m}m {s:02d}s" if m else f"in {s}s"


def status_renderable(s: dict[str, Any] | None, error: str | None, manager: str | None) -> RenderableType:
    t = Table.grid(padding=(0, 1))
    t.add_column(style="dim", no_wrap=True)
    t.add_column()
    if s is None:
        t.add_row("State", "[bold red]DAEMON OFFLINE[/]")
        t.add_row("", f"[dim]{error or ''}[/]")
        t.add_row("", "Press [bold]s[/] to start it with a local audio client.")
        return t
    if s["killswitch"]:
        state = "[bold white on red] KILL SWITCH ENGAGED [/]"
    elif s["busy"]:
        state = "[bold magenta]● INTERROGATION IN PROGRESS[/]"
    elif s["paused"]:
        state = "[yellow]⏸ paused[/]"
    else:
        state = "[green]● enforcing synergy[/]"
    t.add_row("State", state)
    audio = s["audio"]
    t.add_row("Audio", "[red]no client connected[/]" if not audio else "local (in-process)" if audio == "local"
              else f"[green]{audio['name']}[/] since {datetime.fromtimestamp(audio['connected_at']):%H:%M}")
    nxt = s.get("next_interrogation")
    if nxt:
        when = datetime.fromisoformat(nxt)
        t.add_row("Next", f"{when:%a %H:%M} [dim]({_ago(when)})[/]")
    else:
        t.add_row("Next", "[dim]not scheduled[/]")
    llm = s.get("llm_health") or {}
    llm_state = "[green]up[/]" if llm.get("ok") else "[red]down[/]" if llm else "[dim]?[/]"
    t.add_row("LLM", f"{llm_state} [dim]{(s.get('llm_endpoint') or 'disabled').removeprefix('http://')}[/]")
    who = PERSONALITIES[manager].title if manager else "Random"
    t.add_row("Manager", f"[cyan]{who}[/] [dim](m to change)[/]")
    up = s["uptime_seconds"]
    t.add_row("Uptime", f"[dim]{up // 3600}h {up % 3600 // 60}m · v{s['version']}[/]")
    return t


def escalation_renderable(level: int) -> RenderableType:
    t = Table.grid(padding=(0, 1))
    for lvl in range(5, -1, -1):
        style = render.level_style(lvl)
        current = lvl == level
        marker = f"[bold {style}]▶[/]" if current else " "
        block = f"[{style}]{'█' if lvl <= level and lvl else '░'}[/]" if lvl else "[green]●[/]"
        name = f"[bold {style}]{LEVEL_NAMES[lvl]}[/]" if current else f"[dim]{LEVEL_NAMES[lvl]}[/]"
        t.add_row(marker, block, f"[dim]{lvl}[/]", name)
    return t


def reputation_renderable(s: dict[str, Any]) -> RenderableType:
    rank_no, points = s["rank_number"], s["synergy_points"]
    lo = RANKS[rank_no - 1][0]
    hi = RANKS[rank_no][0] if rank_no < len(RANKS) else None
    lines: list[RenderableType] = [Text.from_markup(f"[bold magenta]{s['rank']}[/] [dim]{rank_no}/{len(RANKS)}[/]")]
    if hi is not None:
        frac = max(0.0, min(1.0, (points - lo) / (hi - lo)))
        width = 22
        filled = round(frac * width)
        lines.append(Text.from_markup(f"[magenta]{'━' * filled}[/][dim]{'━' * (width - filled)}[/] "
                                      f"{points:g} / {hi:g} pts"))
        lines.append(Text.from_markup(f"[dim]Next: {RANKS[rank_no][1]}[/]"))
    else:
        lines.append(Text.from_markup(f"[bold]{points:g} pts[/] [dim]· nowhere left to ascend[/]"))
    g = Table.grid(padding=(0, 1))
    g.add_column(style="dim")
    g.add_column()
    g.add_row("Reports", f"[green]{s['successful_reports']} passed[/] · [red]{s['failed_evaluations']} failed[/]")
    g.add_row("Streak", f"{s['consecutive_successes']} [dim](best {s['best_streak']})[/]")
    g.add_row("Avg score", f"{s['average_score']} [dim]· {s['average_jargon_density'] * 100:.0f}% jargon[/]")
    g.add_row("Longest win", f"{s['longest_successful_monologue_words']} words")
    g.add_row("PIPs", str(s["pips_issued"]))
    g.add_row("Talked over", f"{s.get('times_talked_over_management', 0)}× [dim]management[/]")
    lines.append(g)
    if s.get("open_pip"):
        lines.append(Text.from_markup("[bold red]Open PIP:[/] " + ", ".join(s["open_pip"]["terms"])))
    return Group(*lines)


def buzzwords_renderable(top: list[list[Any]]) -> RenderableType:
    if not top:
        return Text.from_markup("[dim]No buzzwords leveraged yet.[/]")
    peak = max(n for _, n in top) or 1
    t = Table.grid(padding=(0, 1))
    t.add_column(no_wrap=True)
    t.add_column(justify="right", style="dim")
    for term, n in top[:8]:
        t.add_row(f"[magenta]{'▇' * max(1, round(n / peak * 6))}[/] {term}", str(n))
    return t


class ManagementConsole(App[None]):
    TITLE = "middlemanagementd"
    SUB_TITLE = "Corporate Alignment Enforcement Console"

    CSS = """
    Screen { layout: horizontal; }
    #side { width: 44; }
    #main { width: 1fr; }
    .panel { border: round $primary-darken-1; padding: 0 1; height: auto; }
    .panel:focus-within { border: round $accent; }
    #status { border: round magenta; }
    #feed { height: 1fr; border: round cyan; }
    #review { height: auto; max-height: 16; border: round magenta; }
    #history { height: 12; border: round $primary-darken-1; }
    #buzzwords { height: 1fr; }
    """

    BINDINGS = [
        Binding("t", "trigger", "Interrogate me"),
        Binding("m", "cycle_manager", "Manager"),
        Binding("p", "toggle_pause", "Pause/Resume"),
        Binding("k", "kill", "KILL", priority=True),
        Binding("r", "revive", "Revive"),
        Binding("h", "hush", "Hush"),
        Binding("s", "start", "Start daemon"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, api: ApiCall, *, start: Callable[[], list[str]] | None = None,
                 hush_local: Callable[[], bool] | None = None, poll_seconds: float = 0.5) -> None:
        super().__init__()
        self.api = api
        self._start = start
        self._hush_local = hush_local
        self.poll_seconds = poll_seconds
        self.status: dict[str, Any] | None = None
        self.offline_reason: str | None = None
        self.seq: int | None = None
        self.manager_idx = 0
        self._polling = False

    # --- layout ------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="side"):
            yield Static(id="status", classes="panel")
            yield Static(id="escalation", classes="panel")
            yield Static(id="reputation", classes="panel")
            yield Static(id="buzzwords", classes="panel")
        with Vertical(id="main"):
            yield RichLog(id="feed", wrap=True, markup=True, highlight=False, min_width=40)
            yield Static(Text.from_markup("[dim]No evaluations yet. Press [bold]t[/] to be interrogated.[/]"),
                         id="review", classes="panel")
            yield DataTable(id="history", cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        titles = {"status": "Status", "escalation": "Escalation", "reputation": "Corporate Reputation",
                  "buzzwords": "Most Leveraged Buzzwords", "feed": "Live Interrogation",
                  "review": "Last Performance Review", "history": "History"}
        for wid, title in titles.items():
            self.query_one(f"#{wid}").border_title = title
        self.query_one("#history", DataTable).add_columns("When", "Manager", "Score", "Result", "Level", "Said")
        self.query_one("#escalation", Static).update(escalation_renderable(0))
        self.call_after_refresh(self.refresh_all)
        self.set_interval(self.poll_seconds, self.poll_events)
        self.set_interval(2.0, self.refresh_status)
        self.set_interval(15.0, self.refresh_reputation)
        self.set_interval(30.0, self.refresh_llm)

    @property
    def manager(self) -> str | None:
        return MANAGERS[self.manager_idx]

    # --- API plumbing ------------------------------------------------------
    async def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        return await asyncio.to_thread(self.api, method, path, body)

    async def refresh_all(self) -> None:
        await self.refresh_status(check_llm=True)
        await self.refresh_reputation()

    async def refresh_llm(self) -> None:
        await self.refresh_status(check_llm=True)

    async def refresh_status(self, check_llm: bool = False) -> None:
        from .cli import ApiError

        try:
            prev_llm = (self.status or {}).get("llm_health")
            s = await self.call("GET", "/api/status?check=1" if check_llm else "/api/status")
            s.setdefault("llm_health", prev_llm)
            was_offline = self.status is None
            self.status, self.offline_reason = s, None
            if was_offline:
                self.sub_title = "Corporate Alignment Enforcement Console"
                await self.refresh_reputation()
        except ApiError as e:
            self.status, self.offline_reason = None, str(e)
            self.sub_title = "daemon offline"
        self.query_one("#status", Static).update(status_renderable(self.status, self.offline_reason, self.manager))
        if self.status:
            self.query_one("#escalation", Static).update(escalation_renderable(self.status["escalation_level"]))

    async def refresh_reputation(self) -> None:
        from .cli import ApiError

        try:
            stats = await self.call("GET", "/api/stats")
            history = await self.call("GET", "/api/history?limit=25")
        except ApiError:
            return
        self.query_one("#reputation", Static).update(reputation_renderable(stats))
        self.query_one("#buzzwords", Static).update(buzzwords_renderable(stats["top_buzzwords"]))
        self.query_one("#escalation", Static).update(escalation_renderable(stats["escalation_level"]))
        table = self.query_one("#history", DataTable)
        table.clear()
        for r in history:
            table.add_row(
                datetime.fromtimestamp(r["ts"]).strftime("%m-%d %H:%M"),
                PERSONALITIES[r["personality"]].title if r["personality"] in PERSONALITIES else r["personality"],
                f"{r['overall']:.0f}",
                Text("PASS", style="bold green") if r["passed"] else Text("FAIL", style="bold red"),
                f"{r['level_before']}→{r['level_after']}",
                (r["transcript"] or "")[:80],
            )

    async def poll_events(self) -> None:
        from .cli import ApiError

        if self._polling or self.status is None:
            return
        self._polling = True
        try:
            if self.seq is None:
                # First contact: replay a little recent history for context.
                self.seq = max(0, int(self.status.get("last_event_seq", 0)) - FEED_BACKLOG)
            events = await self.call("GET", f"/api/events?since={self.seq}")
            for e in events:
                self.seq = max(self.seq, e["seq"])
                self.show_event(e)
            if events and any(e["type"] in ("session_finished", "rank_change", "killswitch", "conceded")
                              for e in events):
                await self.refresh_reputation()
                await self.refresh_status()
        except ApiError:
            pass
        finally:
            self._polling = False

    def show_event(self, e: dict[str, Any]) -> None:
        feed = self.query_one("#feed", RichLog)
        if e["type"] == "evaluation":
            review = self.query_one("#review", Static)
            review.update(Group(render.score_table(e["data"]["scores"], width=24), Text(),
                                render.evaluation_summary(e["data"])))
            review.border_title = f"Last Performance Review · {datetime.fromtimestamp(e['ts']):%H:%M}"
            if (r := render.event(e, include_evaluation=False)) is not None:
                feed.write(r)
            return
        if e["type"] == "session_started":
            self.notify(f"{e['data']['personality']} would like a quick word.", title="Interrogation", timeout=6)
        if (r := render.event(e)) is not None:
            feed.write(r)

    # --- actions -------------------------------------------------------------
    async def _post(self, path: str, body: dict[str, Any] | None = None, *, quiet: bool = False) -> dict[str, Any] | None:
        from .cli import ApiError

        try:
            res = await self.call("POST", path, body)
        except ApiError as e:
            self.notify(str(e), severity="error")
            return None
        if not quiet:
            ok = res.get("ok", False)
            self.notify(res.get("message", ""), severity="information" if ok else "warning")
        await self.refresh_status()
        return res

    async def action_trigger(self) -> None:
        await self._post("/api/trigger", {"personality": self.manager} if self.manager else {})

    async def action_cycle_manager(self) -> None:
        self.manager_idx = (self.manager_idx + 1) % len(MANAGERS)
        self.query_one("#status", Static).update(status_renderable(self.status, self.offline_reason, self.manager))

    async def action_toggle_pause(self) -> None:
        paused = bool(self.status and self.status["paused"])
        await self._post("/api/resume" if paused else "/api/pause")

    async def action_kill(self) -> None:
        await self._post("/api/kill")

    async def action_revive(self) -> None:
        await self._post("/api/revive")

    async def action_hush(self) -> None:
        local = bool(self._hush_local and await asyncio.to_thread(self._hush_local))
        res = await self._post("/api/kill", quiet=True)
        parts = ["local audio silenced" if local else "no local audio client"]
        if res is not None:
            parts.append("kill switch engaged (r to revive)")
        self.notify("; ".join(parts), title="Hush", severity="warning")

    async def action_start(self) -> None:
        if self.status is not None:
            self.notify("Daemon is already running.")
            return
        if self._start is None:
            self.notify("Starting isn't available from here.", severity="warning")
            return
        from .cli import ApiError

        self.notify("Starting daemon and audio client...", timeout=3)
        try:
            for msg in await asyncio.to_thread(self._start):
                self.notify(Text.from_markup(msg).plain, timeout=5)
        except ApiError as e:
            self.notify(str(e), severity="error")
        await self.refresh_all()


def run(cfg: Config) -> int:
    from .cli import api, signal_audio_client, start_background

    app = ManagementConsole(
        lambda method, path, body: api(cfg, method, path, body, timeout=15),
        start=lambda: start_background(cfg, with_audio=True),
        hush_local=lambda: signal_audio_client(signal.SIGUSR1),
    )
    app.sub_title = f"Corporate Alignment Enforcement Console · {cfg.server.url}"
    app.run()
    return 0

