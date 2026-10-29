"""Rich renderables shared by the CLI and the TUI."""

from __future__ import annotations

from typing import Any

from datetime import datetime

from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from .escalation import LEVEL_NAMES
from .personalities import PERSONALITIES
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
        return Text.from_markup(f"[red]{d.get('what', 'interrogation').capitalize()} aborted:[/] {d['reason']}")
    if t == "review_started":
        how = "" if d["aloud"] else " [dim](filed silently)[/]"
        return Rule(f"[bold yellow]Weekly performance review with {d['personality']}[/]{how}")
    if t == "review_ready":
        style = rating_style(d["rating"])
        return Text.from_markup(f"[bold]Review filed:[/] [{style}]{d['rating_name']}[/]. [italic]{d['headline']}[/]")
    if t == "session_finished":
        return Text.from_markup(f"[dim]Finished in {d['duration_seconds']}s. Rank: {d['rank_after']}[/]")
    if t == "killswitch":
        return Text.from_markup("[bold red]KILL SWITCH ENGAGED[/]" if d["engaged"] else "[green]Kill switch released.[/]")
    if t in ("paused", "resumed"):
        return Text.from_markup(f"[dim]Scheduler {t}.[/]")
    if t == "skipped":
        return Text.from_markup(f"[dim]Scheduled interrogation skipped: {d['reason']}[/]")
    if t == "word_of_day":
        return Text.assemble(("Word of the day! ", "bold green"), f"You worked in “{d['term']}”. ",
                             (f"+{d['points']:g} synergy points.", "green"))
    if t == "coaching":
        return coaching(d)
    if t == "history_changed":
        return Text.from_markup(f"[dim]Records edited: {d['what']} deleted.[/]")
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


def rating_style(rating: int) -> str:
    return {5: "bold green", 4: "green", 3: "yellow", 2: "red", 1: "bold red"}.get(rating, "dim")


def stars(rating: int) -> str:
    return "★" * rating + "☆" * (5 - rating)


def manager_title(key: str) -> str:
    return PERSONALITIES[key].title if key in PERSONALITIES else key


def review(r: dict[str, Any]) -> RenderableType:
    """A filed weekly performance review, as an HR document."""
    rv, sm = r["review"], r["summary"]
    style = rating_style(r["rating"])
    head = Table.grid(padding=(0, 2))
    head.add_column(style="dim")
    head.add_column()
    head.add_row("Week of", sm["week_of"])
    head.add_row("Reviewer", manager_title(r["personality"]))
    head.add_row("Rating", f"[{style}]{stars(r['rating'])}  {r['rating_name']}[/]")
    head.add_row("Filed", datetime.fromtimestamp(r["ts"]).strftime("%a %d %b %H:%M")
                 + ("" if r["delivered"] else " [dim](not read aloud)[/]")
                 + (" [dim]· HR template[/]" if r["author"] == "template" else ""))
    parts: list[RenderableType] = [head, Text(), Text.from_markup(f"[bold italic]{rv['headline']}[/]")]
    for title, key, bullet in (("Key strengths", "strengths", "[green]+[/]"),
                               ("Areas for development", "development_areas", "[red]-[/]"),
                               ("Objectives for next week", "objectives", "[cyan]→[/]")):
        parts.append(Text.from_markup(f"\n[bold magenta]{title}[/]"))
        parts.extend(Text.from_markup(f"{bullet} {line}") for line in rv[key])
    parts.append(Text.from_markup(f"\n[italic]{rv['closing']}[/]"))
    parts.append(Text())
    parts.append(review_facts(sm))
    return Group(*parts)


def review_facts(sm: dict[str, Any]) -> RenderableType:
    t = Table(title="Calibration data", title_style="dim", show_header=False, box=None, padding=(0, 2))
    t.add_column(style="dim")
    t.add_column()
    n = sm["interrogations"]
    t.add_row("Interrogations", str(n) + (f" ([green]{sm['passed']} passed[/], [red]{sm['failed']} failed[/])"
                                          if n else ""))
    if n:
        prev = sm.get("previous_week_average")
        delta = "" if prev is None else f" [dim](last week {prev:g})[/]"
        t.add_row("Average score", f"{sm['average_score']:g}{delta}")
        t.add_row("Best / worst", f"{sm['best_score']:g} / {sm['worst_score']:g}")
        t.add_row("Escalation", f"{sm['escalation_start']} → {sm['escalation_end']} (peak {sm['highest_escalation']})")
        if sm.get("top_buzzwords"):
            t.add_row("Top buzzwords", ", ".join(f"{w} ×{c}" for w, c in sm["top_buzzwords"]))
        t.add_row("Cut off / talked over", f"{sm['times_cut_off']} / {sm['times_talked_over_management']}")
    t.add_row("PIPs issued", str(sm["pips_issued"]))
    t.add_row("Synergy points", f"{sm['synergy_points_change']:+g}")
    rank = sm["rank_at_end"] if sm["rank_at_start"] == sm["rank_at_end"] else f"{sm['rank_at_start']} → {sm['rank_at_end']}"
    t.add_row("Rank", rank)
    return t


def coaching(c: dict[str, Any]) -> RenderableType:
    """The post-interrogation coaching: an executive rewrite (LLM) and/or tips."""
    parts: list[RenderableType] = []
    if c.get("rewrite"):
        parts.append(Text(c["rewrite"], style="italic"))
        if c.get("rewrite_jargon_ratio") is not None:
            parts.append(Text(f"{c['rewrite_jargon_ratio'] * 100:.0f}% jargon", style="dim"))
    if c.get("highlights"):
        parts.append(Text())
        for h in c["highlights"]:
            parts.append(Text.assemble(("• ", "green"), (f"“{h['phrase']}”", "bold green"),
                                       (f"  {h['why']}" if h.get("why") else "", "")))
    if c.get("tips"):
        if parts:
            parts.append(Text())
        for tip in c["tips"]:
            parts.append(Text.assemble(("Tip: ", "bold cyan"), tip))
    title = "How a VP would have said it" if c.get("rewrite") else "Coaching"
    return Panel(Group(*parts), title=title, border_style="green")


def word_of_day(w: dict[str, Any] | None) -> RenderableType:
    if not w:
        return Text("Word of the day is off.", style="dim")
    lines = [Text.assemble((w["term"], "bold green"), "  ",
                           ("✓ earned today" if w.get("claimed_by") else "not used yet", "green" if w.get("claimed_by")
                            else "dim"))]
    lines.append(Text(w.get("definition") or w["hint"], style="" if w.get("definition") else "dim"))
    if w.get("example"):
        lines.append(Text(f"“{w['example']}”", style="italic dim"))
    return Group(*lines)


def interrogation(r: dict[str, Any]) -> RenderableType:
    """One stored interrogation in full."""
    head = Table.grid(padding=(0, 2))
    head.add_column(style="dim")
    head.add_column()
    head.add_row("When", datetime.fromtimestamp(r["ts"]).strftime("%a %d %b %H:%M") + f" [dim]({r['trigger']})[/]")
    head.add_row("Manager", manager_title(r["personality"]))
    head.add_row("Escalation", f"{r['level_before']} → {r['level_after']} "
                               f"[dim]({LEVEL_NAMES.get(r['level_after'], '')})[/]")
    extras = [x for x in ("cut off" if r.get("interrupted") else "",
                          f"talked over management ×{r['barge_ins']}" if r.get("barge_ins") else "") if x]
    if extras:
        head.add_row("Notes", ", ".join(extras))
    transcript = r.get("transcript")
    ev = {**r, "critique": "", "llm_error": None}
    return Group(
        head,
        Panel(Text(r["question"]), title="Question", border_style="cyan"),
        Panel(Text(transcript) if transcript else Text.from_markup("[dim](silence, or transcripts not stored)[/]"),
              title="Your status update", border_style="blue"),
        Panel(score_table(r["scores"], width=24), title="Performance Review", border_style="magenta"),
        evaluation_summary(ev),
        Panel(Text(r.get("feedback") or ""), title="Manager feedback",
              border_style=level_style(r["level_after"])),
        *([coaching(r["coaching"])] if r.get("coaching") else []),
    )
