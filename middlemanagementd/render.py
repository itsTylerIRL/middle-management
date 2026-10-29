"""Rich renderables shared by the CLI and the TUI."""

from __future__ import annotations

from typing import Any

from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from .escalation import LEVEL_NAMES
from .scoring import METRICS


def bar(value: float, width: int = 20) -> str:
    filled = int(round(max(0.0, min(100.0, value)) / 100 * width))
    color = "green" if value >= 70 else "yellow" if value >= 45 else "red"
    return f"[{color}]{'█' * filled}[/][dim]{'░' * (width - filled)}[/] {value:5.1f}"


def level_style(level: int) -> str:
    return "red" if level >= 3 else "yellow" if level else "green"


def score_table(scores: dict[str, float], width: int = 20) -> Table:
    table = Table(show_header=False, box=None, padding=(0, 1))
    for m in METRICS:
        table.add_row(m.replace("_", " ").title(), bar(scores.get(m, 0), width))
    return table


def evaluation_summary(ev: dict[str, Any]) -> Text:
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
    return Text.from_markup("\n".join(lines))


def evaluation(ev: dict[str, Any], transcript: str | None = None) -> RenderableType:
    parts: list[RenderableType] = []
    if transcript is not None:
        parts.append(Panel(transcript or "[dim](silence)[/]", title="Your status update", border_style="blue"))
    parts.append(Panel(score_table(ev["scores"]), title="Performance Review", border_style="magenta"))
    parts.append(evaluation_summary(ev))
    return Group(*parts)


def event(e: dict[str, Any], *, include_evaluation: bool = True) -> RenderableType | None:
    """One daemon event as a renderable (None for events not worth showing)."""
    t, d = e["type"], e["data"]
    if t == "session_started":
        return Rule(f"[bold magenta]Interrogation by {d['personality']}[/]")
    if t == "question":
        return Text.from_markup(f"[bold cyan]{d['personality']}:[/] {d['text']}")
    if t == "transcript":
        note = " [red](interrupted)[/]" if d.get("interrupted") else ""
        return Panel(d["text"] or "[dim](silence)[/]", title=f"You{note}", border_style="blue")
    if t == "evaluation":
        return evaluation(d) if include_evaluation else evaluation_summary(d)
    if t == "feedback":
        level = d.get("level", 0)
        return Panel(d["text"], title=f"Feedback: level {level} {LEVEL_NAMES.get(level, '')}",
                     border_style=level_style(level))
    if t == "barge_in":
        if d["mode"] == "talk_over":
            return Text.from_markup("[bold red]You talked over management.[/] [dim]Management is now louder.[/]")
        where = " on the question" if d["phase"] == "question" else ""
        return Text.from_markup(f"[yellow]You cut in{where}:[/] {d.get('text') or '(inaudible)'}")
    if t == "conceded":
        return Text.from_markup(f"[bold green]Management concedes.[/] Escalation reduced to level {d['level']}.")
    if t == "rank_change":
        return Text.from_markup(f"[bold]Title change:[/] {d['from']} → [bold green]{d['to']}[/]")
    if t == "session_aborted":
        return Text.from_markup(f"[red]Interrogation aborted:[/] {d['reason']}")
    if t == "session_finished":
        return Text.from_markup(f"[dim]Finished in {d['duration_seconds']}s. Rank: {d['rank_after']}[/]")
    if t == "killswitch":
        return Text.from_markup("[bold red]KILL SWITCH ENGAGED[/]" if d["engaged"] else "[green]Kill switch released.[/]")
    if t in ("paused", "resumed"):
        return Text.from_markup(f"[dim]Scheduler {t}.[/]")
    if t == "skipped":
        return Text.from_markup(f"[dim]Scheduled interrogation skipped: {d['reason']}[/]")
    if t == "audio_client":
        state = "connected" if d["connected"] else "[red]disconnected[/]"
        return Text.from_markup(f"[dim]Audio client {d['name']} {state}.[/]")
    return None


def stats_tables(s: dict[str, Any]) -> list[Table]:
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
    t.add_row("Times you talked over management", str(s.get("times_talked_over_management", 0)))
    if s.get("open_pip"):
        t.add_row("[red]Open PIP[/]", ", ".join(s["open_pip"]["terms"]))
    tables = [t]
    if s["top_buzzwords"]:
        b = Table(title="Most Leveraged Buzzwords", title_style="bold magenta")
        b.add_column("Term")
        b.add_column("Uses", justify="right")
        for term, n in s["top_buzzwords"]:
            b.add_row(term, str(n))
        tables.append(b)
    return tables
