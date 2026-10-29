"""TUI behaviour against a fake daemon API, headless via Textual's pilot."""

import os
import time
from typing import Any

from textual.widgets import DataTable, RichLog, Static

from middlemanagementd.cli import ApiError
from middlemanagementd.tui import ManagementConsole, escalation_renderable, reputation_renderable

STATUS = {
    "version": "0.1.0", "uptime_seconds": 3723, "paused": False, "killswitch": False, "busy": False,
    "scheduler_enabled": True, "next_interrogation": "2099-01-01T09:00:00",
    "audio": {"name": "desk", "connected_at": time.time()}, "escalation_level": 2,
    "escalation_name": "Alignment Concerns", "rank": "Associate Synergy Coordinator",
    "llm_endpoint": "http://127.0.0.1:8080/v1", "last_event_seq": 3, "llm_health": {"ok": True},
}
STATS = {
    "total_interrogations": 4, "successful_reports": 3, "failed_evaluations": 1, "average_jargon_density": 0.31,
    "average_score": 66.2, "longest_successful_monologue_words": 88, "top_buzzwords": [["synergy", 4], ["north star", 2]],
    "rank_number": 2, "rank": "Associate Synergy Coordinator", "synergy_points": 30.5, "points_to_next_rank": 29.5,
    "consecutive_successes": 1, "best_streak": 2, "pips_issued": 0, "times_talked_over_management": 3,
    "escalation_level": 2, "open_pip": None,
}
EVAL = {"scores": {"jargon_density": 90, "question_evasion": 80}, "overall": 74.0, "threshold": 58.0, "passed": True,
        "evaluator": "llm+rules", "word_count": 46, "jargon_ratio": 0.52, "detected_jargon": ["synergy"],
        "critique": "Superb nothing."}
EVENTS = [
    {"seq": 1, "ts": time.time(), "type": "session_started", "data": {"personality": "Big Four Consultant"}},
    {"seq": 2, "ts": time.time(), "type": "question", "data": {"personality": "Big Four Consultant", "text": "Go."}},
    {"seq": 3, "ts": time.time(), "type": "evaluation", "data": EVAL},
]
HISTORY = [
    {"id": 2, "ts": time.time(), "trigger": "manual", "personality": "big_four", "question": "Go.",
     "transcript": "We are leveraging synergies.", "overall": 74.0, "passed": True, "level_before": 2,
     "level_after": 0, **{k: EVAL[k] for k in ("scores", "threshold", "evaluator", "word_count", "jargon_ratio",
                                                "detected_jargon")},
     "feedback": "Visionary.", "interrupted": False, "barge_ins": 1},
    {"id": 1, "ts": time.time() - 3600, "trigger": "scheduled", "personality": "linkedin", "question": "Wins?",
     "transcript": "I fixed the bug.", "overall": 31.0, "passed": False, "level_before": 1, "level_after": 2,
     "scores": {}, "threshold": 58.0, "evaluator": "rules", "word_count": 4, "jargon_ratio": 0.0,
     "detected_jargon": [], "feedback": "Concerning.", "interrupted": True, "barge_ins": 0},
]
REVIEWS = [{
    "id": 7, "ts": time.time(), "period_start": 0, "period_end": 1, "personality": "disappointed_vp", "rating": 2,
    "rating_name": "Needs Synergy Improvement", "author": "llm", "trigger": "scheduled", "delivered": True,
    "summary": {"week_of": "Monday 26 October 2026", "interrogations": 2, "passed": 1, "failed": 1,
                "average_score": 52.5, "previous_week_average": None, "best_score": 74, "worst_score": 31,
                "escalation_start": 1, "escalation_end": 0, "highest_escalation": 2,
                "top_buzzwords": [["synergy", 1]], "times_cut_off": 1, "times_talked_over_management": 1,
                "pips_issued": 0, "synergy_points_change": 7.2, "rank_at_start": "Unaligned Individual Contributor",
                "rank_at_end": "Associate Synergy Coordinator"},
    "review": {"headline": "A week of directional impact.", "strengths": ["Synergy, leveraged."],
               "development_areas": ["You fixed a bug. Why?"], "objectives": ["Align the alignment."],
               "closing": "Let's circle back."},
}]


class FakeDaemon:
    def __init__(self, online: bool = True) -> None:
        self.online = online
        self.calls: list[tuple[str, str, Any]] = []
        self.status = dict(STATUS)
        self.reviews: list[dict[str, Any]] = []
        self.events = list(EVENTS)

    def __call__(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        self.calls.append((method, path, body))
        if not self.online:
            raise ApiError("daemon not reachable at http://127.0.0.1:8742")
        if path.startswith("/api/status"):
            return self.status
        if path == "/api/stats":
            return STATS
        if path.startswith("/api/history"):
            return HISTORY
        if path.startswith("/api/events"):
            since = int(path.split("since=")[1])
            return [e for e in self.events if e["seq"] > since]
        if path.startswith("/api/reviews"):
            return self.reviews
        if path == "/api/pause":
            self.status["paused"] = True
        if path == "/api/kill":
            self.status["killswitch"] = True
        return {"ok": True, "message": "ok"}

    def posts(self) -> list[tuple[str, Any]]:
        return [(p, b) for m, p, b in self.calls if m == "POST"]


def text_of(app: ManagementConsole, wid: str) -> str:
    from rich.console import Console

    console = Console(width=100, record=True)
    console.print(app.query_one(wid, Static).content)
    return console.export_text()


def save_screenshot(app: ManagementConsole, name: str) -> None:
    """Set MMD_SCREENSHOT_DIR to save SVG screenshots (used for the README)."""
    if directory := os.environ.get("MMD_SCREENSHOT_DIR"):
        app.save_screenshot(f"{name}.svg", path=directory)


async def settle(pilot, app) -> None:
    await app.refresh_all()
    await app.poll_events()
    await pilot.pause()


async def test_dashboard_populates_from_daemon():
    api = FakeDaemon()
    app = ManagementConsole(api, poll_seconds=60)
    async with app.run_test(size=(160, 50)) as pilot:
        await settle(pilot, app)
        assert "enforcing synergy" in text_of(app, "#status")
        assert "desk" in text_of(app, "#status")
        assert "Associate Synergy Coordinator" in text_of(app, "#reputation")
        assert "3×" in text_of(app, "#reputation")  # talked over management
        assert "synergy" in text_of(app, "#buzzwords")
        assert "PASS" in text_of(app, "#review")
        assert app.query_one("#history", DataTable).row_count == 2
        assert "▇" in text_of(app, "#reputation") or "▆" in text_of(app, "#reputation")  # score trend
        assert len(app.query_one("#feed", RichLog).lines) > 0
        save_screenshot(app, "dashboard")


async def test_keys_drive_the_daemon():
    api = FakeDaemon()
    app = ManagementConsole(api, poll_seconds=60)
    async with app.run_test(size=(160, 50)) as pilot:
        await settle(pilot, app)
        await pilot.press("t")
        await pilot.press("m", "m", "t")
        await pilot.press("p")
        await pilot.press("k")
        await pilot.press("r")
        await pilot.pause()
        posts = api.posts()
        assert posts[0] == ("/api/trigger", {})
        assert posts[1] == ("/api/trigger", {"personality": "big_four"})  # random -> passive_aggressive -> big_four
        assert ("/api/pause", None) in posts
        assert ("/api/kill", None) in posts and ("/api/revive", None) in posts


async def test_hush_signals_local_client_and_engages_kill_switch():
    api = FakeDaemon()
    hushed = []
    app = ManagementConsole(api, hush_local=lambda: hushed.append(1) or True, poll_seconds=60)
    async with app.run_test(size=(160, 50)) as pilot:
        await settle(pilot, app)
        await pilot.press("h")
        await pilot.pause()
        assert hushed == [1] and ("/api/kill", None) in api.posts()


async def test_offline_then_start():
    api = FakeDaemon(online=False)
    def start() -> list[str]:
        api.online = True
        return ["[green]started[/]"]
    app = ManagementConsole(api, start=start, poll_seconds=60)
    async with app.run_test(size=(160, 50)) as pilot:
        await settle(pilot, app)
        assert "DAEMON OFFLINE" in text_of(app, "#status")
        await pilot.press("s")
        await pilot.pause()
        await app.refresh_status()
        assert "enforcing synergy" in text_of(app, "#status")


def test_escalation_ladder_marks_current_level():
    from rich.console import Console

    c = Console(width=60, record=True)
    c.print(escalation_renderable(4))
    out = c.export_text()
    assert "▶" in out.splitlines()[1] and "Corporate Meltdown" in out.splitlines()[1]


def test_reputation_at_top_rank():
    from rich.console import Console

    c = Console(width=60, record=True)
    c.print(reputation_renderable(STATS | {"rank_number": 8, "rank": "Global Thought Leadership Evangelist",
                                           "synergy_points": 600}))
    assert "nowhere left to ascend" in c.export_text()


async def test_history_tab_shows_full_interrogation():
    api = FakeDaemon()
    app = ManagementConsole(api, poll_seconds=60)
    async with app.run_test(size=(160, 50)) as pilot:
        await settle(pilot, app)
        # Enter on the dashboard's second row opens that interrogation in the History tab.
        table = app.query_one("#history", DataTable)
        table.focus()
        await pilot.press("down", "enter")
        await pilot.pause()
        assert app.query_one("#tabs").active == "history-tab"
        detail = text_of(app, "#detail")
        assert "I fixed the bug." in detail and "Concerning." in detail and "cut off" in detail
        await pilot.press("up")
        await pilot.pause()
        assert "We are leveraging synergies." in text_of(app, "#detail")
        assert "talked over management ×1" in text_of(app, "#detail")
        await pilot.press("1")
        await pilot.pause()
        assert app.query_one("#tabs").active == "dashboard"


async def test_reviews_tab_and_request():
    api = FakeDaemon()
    app = ManagementConsole(api, poll_seconds=60)
    async with app.run_test(size=(160, 50)) as pilot:
        await settle(pilot, app)
        await pilot.press("3")
        assert "No performance reviews" in text_of(app, "#review-doc")
        await pilot.press("w")
        await pilot.pause()
        assert ("/api/review", {}) in api.posts()
        # The daemon files it; the TUI picks it up from the event stream.
        api.reviews = REVIEWS
        api.events.append({"seq": 4, "ts": time.time(), "type": "review_ready",
                           "data": {"id": 7, "personality": "Disappointed VP", "rating": 2,
                                    "rating_name": "Needs Synergy Improvement",
                                    "headline": "A week of directional impact.", "delivered": True,
                                    "week_of": "Monday 26 October 2026"}})
        await app.poll_events()
        await pilot.pause()
        assert app.query_one("#reviews", DataTable).row_count == 1
        doc = text_of(app, "#review-doc")
        assert "Needs Synergy Improvement" in doc and "Align the alignment." in doc and "★★☆☆☆" in doc
        assert "Unaligned Individual Contributor → Associate Synergy Coordinator" in doc
        save_screenshot(app, "reviews")
