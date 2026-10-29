"""Terminal dashboard (Textual). A pure client of the daemon's HTTP API, like the CLI."""

from __future__ import annotations

import asyncio
import signal
from collections import deque
from collections.abc import Callable
from datetime import datetime
from typing import Any, Self

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Resize
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.widgets import Button, DataTable, Footer, Header, Label, RichLog, Static, TabbedContent, TabPane

from . import render
from .config import Config
from .escalation import LEVEL_NAMES
from .personalities import PERSONALITIES
from .reputation import RANKS

# (method, path, body) -> decoded JSON. Raises ApiError when the daemon is unreachable or refuses.
ApiCall = Callable[[str, str, dict[str, Any] | None], Any]

MANAGERS: list[str | None] = [None, *PERSONALITIES]
FEED_BACKLOG = 40
HISTORY_LIMIT = 100
DASHBOARD_HISTORY = 25
SPARKS = "▁▂▃▄▅▆▇█"


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
    if rv := s.get("next_review"):
        when = datetime.fromisoformat(rv)
        t.add_row("Review", f"{when:%a %H:%M} [dim]({_ago(when)})[/]")
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


def trend_renderable(history: list[dict[str, Any]], width: int = 24) -> Text:
    """Recent overall scores, oldest first, as a sparkline: green passes, red failures."""
    recent = list(reversed(history[:width]))
    text = Text()
    for r in recent:
        level = min(len(SPARKS) - 1, int(max(0.0, r["overall"]) / 100 * len(SPARKS)))
        text.append(SPARKS[level], style="green" if r["passed"] else "red")
    return text


def reputation_renderable(s: dict[str, Any], history: list[dict[str, Any]] | None = None) -> RenderableType:
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
    if history:
        g.add_row("Trend", trend_renderable(history))
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


class ReflowLog(RichLog):
    """A RichLog that keeps what it was given and re-renders it when its width changes.

    RichLog renders each write to fixed-width strips, so panels and rules stay frozen at the width the
    window had when they arrived. Replaying the original renderables lets them track the terminal.
    """

    def __init__(self, *args: Any, backlog: int = 500, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._entries: deque[tuple[RenderableType | object, dict[str, Any]]] = deque(maxlen=backlog)
        self._rendered_width = 0
        self._reflow_timer: Timer | None = None

    def write(self, content: RenderableType | object, **kwargs: Any) -> Self:  # type: ignore[override]
        kwargs.setdefault("expand", True)
        self._entries.append((content, kwargs))
        return super().write(content, **kwargs)

    def clear(self) -> Self:
        self._entries.clear()
        return super().clear()

    def on_resize(self, event: Resize) -> None:
        # RichLog's own handler flushes writes made before the size was known; that render counts.
        first = not self._size_known
        super().on_resize(event)
        if first:
            self._rendered_width = event.size.width
        elif event.size.width != self._rendered_width:
            # Debounce: dragging a window edge sends a burst of resizes.
            if self._reflow_timer is not None:
                self._reflow_timer.stop()
            self._reflow_timer = self.set_timer(0.05, self._reflow)

    def _reflow(self) -> None:
        self._reflow_timer = None
        self._rendered_width = self.size.width
        at_end = self.is_vertical_scroll_end
        entries = list(self._entries)
        super().clear()
        for content, kwargs in entries:
            super().write(content, **{**kwargs, "scroll_end": False})
        if at_end:
            self.scroll_end(animate=False, immediate=True, x_axis=False)


class Confirm(ModalScreen[bool]):
    """Yes/no dialog for destructive actions. Defaults to no."""

    DEFAULT_CSS = """
    Confirm { align: center middle; }
    #dialog { width: 64; height: auto; border: thick $error; background: $surface; padding: 1 2; }
    #question { width: 1fr; margin-bottom: 1; }
    #buttons { width: 1fr; height: auto; align-horizontal: right; }
    #buttons Button { margin-left: 2; }
    """
    BINDINGS = [Binding("y", "answer(True)", "Yes"), Binding("n,escape", "answer(False)", "No")]

    def __init__(self, question: str, yes: str = "Delete") -> None:
        super().__init__()
        self.question = question
        self.yes = yes

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(Text.from_markup(self.question), id="question")
            with Horizontal(id="buttons"):
                yield Button("Cancel [dim](n)[/]", id="no")
                yield Button(f"{self.yes} [dim](y)[/]", id="yes", variant="error")

    def on_mount(self) -> None:
        self.query_one("#no", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_answer(self, yes: bool) -> None:
        self.dismiss(yes)


class ManagementConsole(App[None]):
    TITLE = "middlemanagementd"
    SUB_TITLE = "Corporate Alignment Enforcement Console"

    CSS = """
    TabPane { padding: 0; }
    #dash { height: 1fr; }
    #side { width: 44; }
    #main { width: 1fr; }
    .panel { border: round $primary-darken-1; padding: 0 1; height: auto; }
    .panel:focus-within { border: round $accent; }
    #status { border: round magenta; }
    #word { border: round green; }
    #feed { height: 1fr; border: round cyan; }
    #review { height: auto; max-height: 16; border: round magenta; }
    #history { height: 12; border: round $primary-darken-1; }
    #buzzwords { height: 1fr; }
    .list { width: 62; height: 1fr; border: round $primary-darken-1; }
    .doc { width: 1fr; height: 1fr; border: round magenta; padding: 0 1; }
    """

    BINDINGS = [
        Binding("t", "trigger", "Interrogate me"),
        Binding("w", "review", "Weekly review"),
        Binding("m", "cycle_manager", "Manager"),
        Binding("p", "toggle_pause", "Pause/Resume"),
        Binding("k", "kill", "KILL", priority=True),
        Binding("r", "revive", "Revive"),
        Binding("h", "hush", "Hush"),
        Binding("s", "start", "Start daemon"),
        Binding("d", "delete", "Delete"),
        Binding("D", "reset", "Reset all", show=False),
        Binding("q", "quit", "Quit"),
        Binding("1", "tab('dashboard')", "Dashboard", show=False),
        Binding("2", "tab('history')", "History", show=False),
        Binding("3", "tab('reviews')", "Reviews", show=False),
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
        self.history: list[dict[str, Any]] = []
        self.reviews: list[dict[str, Any]] = []

    # --- layout ------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with TabbedContent(id="tabs", initial="dashboard"):
            with TabPane("Dashboard [dim]1[/]", id="dashboard"), Horizontal(id="dash"):
                with Vertical(id="side"):
                    yield Static(id="status", classes="panel")
                    yield Static(id="word", classes="panel")
                    yield Static(id="escalation", classes="panel")
                    yield Static(id="reputation", classes="panel")
                    yield Static(id="buzzwords", classes="panel")
                with Vertical(id="main"):
                    yield ReflowLog(id="feed", wrap=True, markup=True, highlight=False, min_width=40)
                    yield Static(Text.from_markup("[dim]No evaluations yet. Press [bold]t[/] to be interrogated.[/]"),
                                 id="review", classes="panel")
                    yield DataTable(id="history", cursor_type="row", zebra_stripes=True)
            with TabPane("History [dim]2[/]", id="history-tab"), Horizontal():
                yield DataTable(id="history-full", cursor_type="row", zebra_stripes=True, classes="list")
                with VerticalScroll(id="detail-scroll", classes="doc"):
                    yield Static(Text.from_markup("[dim]No interrogations on file.[/]"), id="detail")
            with TabPane("Reviews [dim]3[/]", id="reviews-tab"), Horizontal():
                yield DataTable(id="reviews", cursor_type="row", zebra_stripes=True, classes="list")
                with VerticalScroll(id="review-scroll", classes="doc"):
                    yield Static(Text.from_markup(
                        "[dim]No performance reviews on file. Press [bold]w[/] to request one, or wait for "
                        "Friday afternoon.[/]"), id="review-doc")
        yield Footer()

    def on_mount(self) -> None:
        titles = {"status": "Status", "word": "Word of the Day", "escalation": "Escalation", "reputation": "Corporate Reputation",
                  "buzzwords": "Most Leveraged Buzzwords", "feed": "Live Interrogation",
                  "review": "Last Evaluation", "history": "History [dim](enter for details)[/]",
                  "history-full": "Interrogations", "detail-scroll": "Details",
                  "reviews": "Weekly Performance Reviews", "review-scroll": "Review"}
        for wid, title in titles.items():
            self.query_one(f"#{wid}").border_title = title
        self.query_one("#history", DataTable).add_columns("When", "Manager", "Score", "Result", "Level", "Said")
        self.query_one("#history-full", DataTable).add_columns("When", "Manager", "Score", "Result", "Level")
        self.query_one("#reviews", DataTable).add_columns("Week of", "Rating", "Reviewer")
        self.query_one("#escalation", Static).update(escalation_renderable(0))
        self.call_after_refresh(self.refresh_all)
        self.set_interval(self.poll_seconds, self.poll_events)
        self.set_interval(2.0, self.refresh_status)
        self.set_interval(15.0, self.refresh_reputation)
        self.set_interval(30.0, self.refresh_llm)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        # Under a confirm dialog only the safety keys stay live (and `d` can't stack a second dialog).
        if isinstance(self.screen, Confirm):
            return action in ("kill", "hush", "quit")
        return True

    @property
    def manager(self) -> str | None:
        return MANAGERS[self.manager_idx]

    # --- API plumbing ------------------------------------------------------
    async def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        return await asyncio.to_thread(self.api, method, path, body)

    async def refresh_all(self) -> None:
        await self.refresh_status(check_llm=True)
        await self.refresh_reputation()
        await self.refresh_reviews()

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
                await self.refresh_reviews()
        except ApiError as e:
            self.status, self.offline_reason = None, str(e)
            self.sub_title = "daemon offline"
        self.query_one("#status", Static).update(status_renderable(self.status, self.offline_reason, self.manager))
        word = self.query_one("#word", Static)
        word.display = bool(self.status and self.status.get("word_of_day"))
        if self.status:
            self.query_one("#escalation", Static).update(escalation_renderable(self.status["escalation_level"]))
            word.update(render.word_of_day(self.status.get("word_of_day")))

    async def refresh_reputation(self) -> None:
        from .cli import ApiError

        try:
            stats = await self.call("GET", "/api/stats")
            history = await self.call("GET", f"/api/history?limit={HISTORY_LIMIT}")
        except ApiError:
            return
        self.history = history
        self.query_one("#reputation", Static).update(reputation_renderable(stats, history))
        self.query_one("#buzzwords", Static).update(buzzwords_renderable(stats["top_buzzwords"]))
        self.query_one("#escalation", Static).update(escalation_renderable(stats["escalation_level"]))
        table = self.query_one("#history", DataTable)
        full = self.query_one("#history-full", DataTable)
        keep = full.cursor_row
        table.clear()
        full.clear()
        for i, r in enumerate(history):
            cells = (
                datetime.fromtimestamp(r["ts"]).strftime("%m-%d %H:%M"),
                render.manager_title(r["personality"]),
                f"{r['overall']:.0f}",
                Text("PASS", style="bold green") if r["passed"] else Text("FAIL", style="bold red"),
                f"{r['level_before']}→{r['level_after']}",
            )
            if i < DASHBOARD_HISTORY:
                table.add_row(*cells, (r["transcript"] or "")[:80], key=str(r["id"]))
            full.add_row(*cells, key=str(r["id"]))
        if history:
            full.move_cursor(row=min(keep, len(history) - 1))
            self.show_interrogation(full.cursor_row)
        else:
            self.query_one("#detail", Static).update(Text.from_markup("[dim]No interrogations on file.[/]"))

    async def refresh_reviews(self) -> None:
        from .cli import ApiError

        try:
            reviews = await self.call("GET", "/api/reviews?limit=52")
        except ApiError:
            return
        self.reviews = reviews
        table = self.query_one("#reviews", DataTable)
        keep = table.cursor_row
        table.clear()
        for r in reviews:
            table.add_row(r["summary"]["week_of"].split(" ", 1)[1] if " " in r["summary"]["week_of"]
                          else r["summary"]["week_of"],
                          Text(render.stars(r["rating"]), style=render.rating_style(r["rating"])),
                          render.manager_title(r["personality"]), key=str(r["id"]))
        if reviews:
            table.move_cursor(row=min(keep, len(reviews) - 1))
            self.show_review(table.cursor_row)
        else:
            self.query_one("#review-doc", Static).update(Text.from_markup("[dim]No performance reviews on file.[/]"))

    def show_interrogation(self, row: int) -> None:
        if 0 <= row < len(self.history):
            self.query_one("#detail", Static).update(render.interrogation(self.history[row]))

    def show_review(self, row: int) -> None:
        if 0 <= row < len(self.reviews):
            self.query_one("#review-doc", Static).update(render.review(self.reviews[row]))

    def on_data_table_row_highlighted(self, message: DataTable.RowHighlighted) -> None:
        if message.data_table.id == "history-full":
            self.show_interrogation(message.cursor_row)
        elif message.data_table.id == "reviews":
            self.show_review(message.cursor_row)

    def on_data_table_row_selected(self, message: DataTable.RowSelected) -> None:
        if message.data_table.id == "history":
            # Dashboard row -> the same interrogation in the History tab.
            self.action_tab("history")
            full = self.query_one("#history-full", DataTable)
            full.move_cursor(row=message.cursor_row)
            self.show_interrogation(message.cursor_row)

    def action_tab(self, tab: str) -> None:
        pane, focus = {"dashboard": ("dashboard", "#history"), "history": ("history-tab", "#history-full"),
                       "reviews": ("reviews-tab", "#reviews")}[tab]
        self.query_one("#tabs", TabbedContent).active = pane
        # Focus must follow, or Textual switches back to the tab that still holds it.
        self.query_one(focus).focus()

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
            if events and any(e["type"] in ("session_finished", "rank_change", "killswitch", "conceded",
                                                         "history_changed")
                              for e in events):
                await self.refresh_reputation()
                await self.refresh_status()
            if any(e["type"] in ("review_ready", "history_changed") for e in events):
                await self.refresh_reviews()
        except ApiError:
            pass
        finally:
            self._polling = False

    def show_event(self, e: dict[str, Any]) -> None:
        feed = self.query_one("#feed", ReflowLog)
        if e["type"] == "evaluation":
            review = self.query_one("#review", Static)
            review.update(Group(render.score_table(e["data"]["scores"], width=24), Text(),
                                render.evaluation_summary(e["data"])))
            review.border_title = f"Last Evaluation · {datetime.fromtimestamp(e['ts']):%H:%M}"
            if (r := render.event(e, include_evaluation=False)) is not None:
                feed.write(r)
            return
        if e["type"] == "session_started":
            self.notify(f"{e['data']['personality']} would like a quick word.", title="Interrogation", timeout=6)
        elif e["type"] == "review_ready":
            d = e["data"]
            self.notify(f"{d['rating_name']}. Press 3 to read it.", title="Performance review filed", timeout=10,
                        severity="information" if d["rating"] >= 3 else "warning")
        if (r := render.event(e)) is not None:
            feed.write(r)

    # --- actions -------------------------------------------------------------
    async def _post(self, path: str, body: dict[str, Any] | None = None, *, quiet: bool = False,
                    method: str = "POST") -> dict[str, Any] | None:
        from .cli import ApiError

        try:
            res = await self.call(method, path, body)
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

    async def action_review(self) -> None:
        await self._post("/api/review", {"personality": self.manager} if self.manager else {})

    def _selected_record(self) -> tuple[str, dict[str, Any]] | None:
        """The interrogation or review highlighted on the current tab."""
        active = self.query_one("#tabs", TabbedContent).active
        table_id, rows, kind = {"dashboard": ("#history", self.history, "interrogation"),
                                "history-tab": ("#history-full", self.history, "interrogation"),
                                "reviews-tab": ("#reviews", self.reviews, "review")}[active]
        row = self.query_one(table_id, DataTable).cursor_row
        return (kind, rows[row]) if 0 <= row < len(rows) else None

    async def _after_edit(self, res: dict[str, Any] | None) -> None:
        if res and res.get("ok"):
            await self.refresh_reputation()
            await self.refresh_reviews()

    def action_delete(self) -> None:
        if (selected := self._selected_record()) is None:
            self.notify("Nothing selected to delete.")
            return
        kind, r = selected
        when = datetime.fromtimestamp(r["ts"]).strftime("%a %d %b %H:%M")
        if kind == "interrogation":
            what = (f"the interrogation from [bold]{when}[/] "
                    f"({render.manager_title(r['personality'])}, {r['overall']:.0f}, "
                    f"{'PASS' if r['passed'] else 'FAIL'})")
            path = f"/api/history/{r['id']}"
        else:
            what = f"the performance review filed [bold]{when}[/] ({r['rating_name']})"
            path = f"/api/reviews/{r['id']}"

        async def done(yes: bool | None) -> None:
            if yes:
                await self._after_edit(await self._post(path, method="DELETE"))

        self.push_screen(Confirm(f"Delete {what}?\n\n[dim]Points, streaks and buzzword counts are recalculated. "
                                 "This can't be undone.[/]"), done)

    def action_reset(self) -> None:
        async def done(yes: bool | None) -> None:
            if not yes:
                return
            res = await self._post("/api/reset")
            if res and res.get("ok"):
                self.query_one("#review", Static).update(
                    Text.from_markup("[dim]No evaluations yet. Press [bold]t[/] to be interrogated.[/]"))
                self.query_one("#review", Static).border_title = "Last Evaluation"
            await self._after_edit(res)

        self.push_screen(Confirm("[bold red]Reset everything?[/]\n\nEvery interrogation, review and PIP is "
                                 "deleted, and your rank, streaks and escalation level start over. "
                                 "This can't be undone.", yes="Reset"), done)

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

