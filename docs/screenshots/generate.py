"""Regenerate the README screenshots: a plausible week of history, rendered by the real console.

    .venv/bin/python docs/screenshots/generate.py

Writes the dashboard/history/reviews .png files the README uses (via `rsvg-convert`; without it, the .svg files). The data goes through the real Store, practice and review code; only the daemon's HTTP layer is
replaced by direct calls. Textual's SVGs use Fira Code: install it, or point FONTCONFIG_FILE at a config that
includes it, before converting.
"""

from __future__ import annotations

import asyncio
import random
import shutil
import subprocess
import sys
import tempfile
import time
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from middlemanagementd.config import Config
from middlemanagementd.daemon import Daemon
from middlemanagementd.practice import rules_coaching
from middlemanagementd.review import period, rate, summarize
from middlemanagementd.scoring import METRICS
from middlemanagementd.tui import ManagementConsole

OUT = Path(__file__).resolve().parent
SIZE = (150, 52)
WORD = {
    "term": "force multiplier", "category": "buzzwords",
    "hint": "A general-purpose buzzword: drop it anywhere to sound current.",
    "definition": "Someone whose main output is making other people's output sound like theirs.",
    "example": "Honestly, this offsite was a force multiplier for our calendar.",
}

# (manager, question, answer, overall, detected jargon, feedback, coaching or None for rules-based tips)
ANSWERS: list[tuple[str, str, str, float, list[str], str, dict[str, Any] | None]] = [
    ("big_four", "Walk me through your value-creation thesis for today, at a high level.",
     "At a high level, we're leveraging cross-functional synergies to drive alignment with our north star. Net-net, "
     "my team is a force multiplier for the deliverables, and the stakeholders have full visibility.",
     83, ["north star", "synergies", "force multiplier", "net-net"],
     "Exceptional. That is precisely the kind of directional thinking that tells me you are ready for a bigger "
     "slide deck. Let's take the rest offline.",
     {"source": "llm", "rewrite_jargon_ratio": 0.46,
      "rewrite": "Net-net, we're operating as a force multiplier across the value chain: leveraging cross-functional "
                 "synergies, de-risking the roadmap and keeping every stakeholder aligned to the north star. I'll "
                 "socialise the learnings and circle back with a refreshed view of the opportunity space.",
      "highlights": [
          {"phrase": "operating as a force multiplier", "why": "Claims credit for everyone else's work."},
          {"phrase": "de-risking the roadmap", "why": "Sounds like prevention; commits to nothing."},
          {"phrase": "a refreshed view of the opportunity space", "why": "A promise to make another slide."}],
      "tips": ["Open with the conclusion; executives never build up to anything."]}),
    ("linkedin", "What wins are you celebrating today?",
     "I fixed the login bug yesterday and merged the pull request. Next I'm writing tests for two modules.",
     31, [], "I'd really like to see you take a more strategic approach to that response.",
     {"source": "llm", "rewrite_jargon_ratio": 0.38,
      "rewrite": "I'm humbled to share that we unblocked a mission-critical authentication journey, unlocking "
                 "frictionless value for our users. Next, we're doubling down on quality as a strategic pillar, "
                 "hardening the platform so we can scale with confidence.",
      "highlights": [
          {"phrase": "a mission-critical authentication journey", "why": "A login bug, promoted to an outcome."},
          {"phrase": "quality as a strategic pillar", "why": "Writing tests, now a company value."},
          {"phrase": "scale with confidence", "why": "Implies a future nobody can check."}],
      "tips": ["Never say what you did; say what it unlocked."]}),
    ("scrum_master", "Quick sync: what's blocking your velocity?",
     "Our velocity is trending in a direction, and I'd like to time-box a deep dive with the stakeholders to "
     "unblock the swim lanes before we circle back on the sprint goal.",
     71, ["velocity", "deep dive", "swim lanes", "circle back"],
     "Love the energy. Let's park that in the parking lot and action it next sprint.", None),
    ("disappointed_vp", "Remind me again what you've delivered this quarter.",
     "Um, sorry, I'm a bit behind. I think the migration is maybe sixty percent done?",
     12, [], "I'm not angry. I'm just noticing a disconnect between your deliverables and our broader priorities.",
     None),
    ("chief_synergy", "How are you synergizing across the org today?",
     "Holistically, the north star remains our guiding light. I'm partnering cross-functionally to operationalize "
     "our synergies and drive a best-in-class paradigm shift at scale.",
     78, ["holistically", "north star", "synergies", "paradigm shift", "at scale"],
     "Now that is synergy. I can feel the alignment from here.", None),
    ("passive_aggressive", "Per my last message, where are we on the deliverables?",
     "Per my last update, the stakeholders are aligned, the deliverables are tracking, and I'll loop back once "
     "we've socialised the learnings.",
     64, ["stakeholders", "deliverables", "loop back", "learnings"],
     "Great, thanks for finally getting back to me. Noted.", None),
    ("big_four", "Give me the executive summary.",
     "Um, I'm a bit behind on the roadmap, sorry. The vendor hasn't replied yet.",
     48, ["roadmap"], "Let's level set. I'd like to see a more strategic framing next time.", None),
]


def scores_for(overall: float, rng: random.Random) -> dict[str, float]:
    return {m: round(max(0.0, min(100.0, overall + rng.uniform(-14, 14))), 1) for m in METRICS}


def seed(daemon: Daemon, now: datetime, rng: random.Random) -> None:
    store, dictionary = daemon.store, daemon.evaluator.dictionary
    threshold = 58.0
    # Oldest first: two quieter earlier weeks, then this week up to now.
    stamps = [now - timedelta(days=d, hours=h) for d in (16, 15, 14, 9, 8, 7) for h in (4, 1)]
    stamps += [now - timedelta(minutes=90 * i) for i in range(23, -1, -1)]
    level = 1
    for n, ts in enumerate(stamps):
        newest = n == len(stamps) - 1
        # The newest two are the ones on screen: the passing answer, and the failure before it.
        idx = 0 if newest else 1 if n == len(stamps) - 2 else rng.randrange(2, len(ANSWERS))
        manager, question, answer, overall, detected, feedback, coaching = ANSWERS[idx]
        overall = overall if idx < 2 else round(overall + rng.uniform(-6, 6), 1)
        passed = overall >= threshold
        before, level = level, max(0, level - 2) if passed else min(4, level + 1)
        ev = {"overall": overall, "threshold": threshold, "passed": passed, "word_count": len(answer.split()),
              "jargon_ratio": round(len(" ".join(detected).split()) / len(answer.split()), 3),
              "evaluator": "llm+rules", "scores": scores_for(overall, rng), "detected_jargon": detected,
              "critique": "Masterfully evasive; zero verifiable claims." if passed else
              "Contained alarming amounts of concrete, checkable information."}
        rid = store.record(trigger="manual" if newest else "scheduled", personality=manager, question=question,
                           transcript=answer, evaluation=ev, interrupted=idx == 3, level_before=before,
                           level_after=level, feedback=feedback, pip_issued=False,
                           barge_ins=1 if idx == 0 else 0, bonus=5.0 if newest else 0.0)
        store.db.execute("UPDATE interrogations SET ts = ? WHERE id = ?", (ts.timestamp(), rid))
        store.set_coaching(rid, coaching or rules_coaching(ev, dictionary, rng))
        if newest:
            store.set("word_of_day", {"date": date.today().isoformat(), **WORD, "claimed_by": rid})
            replay_session(daemon, manager, question, answer, ev, feedback, coaching)
    store.db.commit()
    store.set("barge_ins", 3)

    reviews = [
        (2, "A week of directional energy, directed nowhere in particular.", "scheduled"),
        (1, "We had high hopes. They remain high, and largely hopes.", "scheduled"),
        (0, "A week in which you said remarkably little, remarkably well.", "manual"),
    ]
    for weeks_back, headline, trigger in reviews:
        start, end = period(now - timedelta(weeks=weeks_back))
        summary = summarize(store, start, end + 60)  # the newest interrogation is stamped exactly at `now`
        rating, rating_name = rate(summary)
        review = {
            "headline": headline,
            "strengths": [f"Your {len(summary.get('top_buzzwords') or [])} signature buzzwords demonstrate genuine "
                          "ownership of the narrative.",
                          "You talked over management, which I'm choosing to read as leadership presence."],
            "development_areas": ["On Tuesday you admitted to fixing a bug. We need to socialise less of that.",
                                  "Your silence on Wednesday was strategic, but not strategic enough."],
            "objectives": ["Circle back on circling back.", "Align the north star with a second, more northern star."],
            "closing": "I'm not angry. I'm aligned. Let's keep this conversation going, asynchronously, forever.",
        }
        rid = store.add_review(period_start=start, period_end=end, personality="disappointed_vp", rating=rating,
                               rating_name=rating_name, summary=summary, review=review, author="llm",
                               delivered=True, trigger=trigger)
        filed = now - timedelta(minutes=5) if weeks_back == 0 else datetime.fromtimestamp(end) - timedelta(hours=7)
        store.db.execute("UPDATE reviews SET ts = ? WHERE id = ?", (filed.timestamp(), rid))
    store.db.commit()


def replay_session(daemon: Daemon, manager: str, question: str, answer: str, ev: dict[str, Any], feedback: str,
                   coaching: dict[str, Any] | None) -> None:
    """The newest interrogation as the live feed saw it."""
    title = "Big Four Consultant"
    emit = daemon._emit
    emit("session_started", {"trigger": "manual", "personality": title, "level": 2})
    emit("question", {"personality": title, "text": question})
    emit("transcript", {"text": answer, "interrupted": False, "speech_seconds": 14.2})
    emit("evaluation", {**ev, "llm_error": None})
    emit("barge_in", {"phase": "feedback", "mode": "yield", "text": "Happy to take that offline with stakeholders."})
    emit("conceded", {"level": 0})
    emit("feedback", {"stage": "praise", "level": 0, "text": feedback})
    emit("word_of_day", {"term": WORD["term"], "points": 5.0})
    emit("coaching", coaching or {})
    emit("session_finished", {"duration_seconds": 71, "rank_after": daemon.status()["rank"]})


def make_api(daemon: Daemon, now: datetime):  # noqa: ANN201
    # The console calls from worker threads; the real daemon only touches SQLite from its event loop.
    lock = threading.Lock()

    def api(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        with lock:
            return route(path)

    def route(path: str) -> Any:
        query = dict(p.split("=", 1) for p in path.partition("?")[2].split("&") if "=" in p)
        if path.startswith("/api/status"):
            return daemon.status() | {"audio": {"name": "desk", "connected_at": (now - timedelta(hours=1)).timestamp()},
                                      "llm_health": {"ok": True}}
        if path.startswith("/api/stats"):
            return daemon.store.stats()
        if path.startswith("/api/history"):
            return daemon.store.history(int(query.get("limit", 20)))
        if path.startswith("/api/reviews"):
            return daemon.store.reviews(int(query.get("limit", 20)))
        if path.startswith("/api/events"):
            return [e for e in daemon.events if e["seq"] > int(query.get("since", 0))]
        return {"ok": True, "message": "ok"}

    return api


async def main() -> int:
    now = datetime.now().replace(second=0, microsecond=0)
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config()
        cfg.storage.database_path = str(Path(tmp) / "screenshots.db")
        cfg.scheduler.enabled = False
        daemon = Daemon(cfg)
        daemon.started_at = time.time() - 3723
        daemon.next_fire_at = now + timedelta(minutes=47)
        daemon.next_review_at = datetime.fromtimestamp(period(now)[0]) + timedelta(days=4, hours=16, minutes=55)
        seed(daemon, now, random.Random(7))

        app = ManagementConsole(make_api(daemon, now), poll_seconds=3600)
        async with app.run_test(size=SIZE) as pilot:
            await app.refresh_all()
            await app.poll_events()
            await pilot.pause(0.5)
            app.save_screenshot("dashboard.svg", path=tmp)

            app.action_tab("history")
            await pilot.press("down")  # the failure, with its rewrite
            await pilot.pause(0.3)
            app.save_screenshot("history.svg", path=tmp)

            app.action_tab("reviews")
            await pilot.pause(0.3)
            app.save_screenshot("reviews.svg", path=tmp)
        await daemon.llm.close()
        daemon.store.close()

        for name in ("dashboard", "history", "reviews"):
            svg = Path(tmp) / f"{name}.svg"
            # rsvg collapses runs of spaces in <text> unless told otherwise, which glues columns together.
            svg.write_text(svg.read_text().replace("<svg ", '<svg xml:space="preserve" ', 1))
            if shutil.which("rsvg-convert"):
                subprocess.run(["rsvg-convert", "-w", "1500", "-o", str(OUT / f"{name}.png"), str(svg)], check=True)
                print(OUT / f"{name}.png")
            else:
                shutil.copy(svg, OUT)
                print(f"{OUT / svg.name} (rsvg-convert not found)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
