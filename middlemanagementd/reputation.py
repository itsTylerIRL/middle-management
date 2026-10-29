"""SQLite persistence: interrogation history, buzzword counts, PIPs, daemon state, corporate rank."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

RANKS: tuple[tuple[float, str], ...] = (
    (0, "Unaligned Individual Contributor"),
    (25, "Associate Synergy Coordinator"),
    (60, "Senior Stakeholder Liaison"),
    (110, "Director of Cross-Functional Enablement"),
    (180, "Vice President of Operational Excellence"),
    (270, "Executive Vice President of Strategic Alignment"),
    (380, "Chief Synergy Officer"),
    (520, "Global Thought Leadership Evangelist"),
)

PASS_POINTS_DIVISOR = 8.0  # a pass earns overall/8 points (~8-12)
FAIL_POINTS = -2.0
PIP_POINTS = -15.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS interrogations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    trigger TEXT NOT NULL,
    personality TEXT NOT NULL,
    question TEXT NOT NULL,
    transcript TEXT,
    word_count INTEGER NOT NULL,
    jargon_ratio REAL NOT NULL,
    overall REAL NOT NULL,
    threshold REAL NOT NULL,
    passed INTEGER NOT NULL,
    evaluator TEXT NOT NULL,
    interrupted INTEGER NOT NULL DEFAULT 0,
    level_before INTEGER NOT NULL,
    level_after INTEGER NOT NULL,
    scores_json TEXT NOT NULL,
    jargon_json TEXT NOT NULL,
    feedback TEXT,
    points REAL NOT NULL DEFAULT 0,
    barge_ins INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS buzzwords (
    term TEXT PRIMARY KEY,
    count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS pips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    terms_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    resolved_ts REAL
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    period_start REAL NOT NULL,
    period_end REAL NOT NULL,
    personality TEXT NOT NULL,
    rating INTEGER NOT NULL,
    rating_name TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    review_json TEXT NOT NULL,
    author TEXT NOT NULL,
    trigger TEXT NOT NULL DEFAULT 'scheduled',
    delivered INTEGER NOT NULL DEFAULT 0
);
"""

# Columns added after the first release, applied to existing databases on open.
MIGRATIONS = (
    ("interrogations", "barge_ins", "INTEGER NOT NULL DEFAULT 0"),
)


def rank_for(points: float) -> tuple[int, str]:
    idx = 0
    for i, (threshold, _) in enumerate(RANKS):
        if points >= threshold:
            idx = i
    return idx + 1, RANKS[idx][1]


@dataclass
class Pip:
    id: int
    ts: float
    terms: list[str]
    status: str


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        for table, column, decl in MIGRATIONS:
            cols = {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    # --- key/value state -------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        row = self.db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set(self, key: str, value: Any) -> None:
        self.db.execute("INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (key, json.dumps(value)))
        self.db.commit()

    @property
    def level(self) -> int:
        return int(self.get("escalation_level", 0))

    @level.setter
    def level(self, value: int) -> None:
        self.set("escalation_level", int(value))

    # --- PIPs ------------------------------------------------------------
    def open_pip(self) -> Pip | None:
        row = self.db.execute("SELECT * FROM pips WHERE status = 'open' ORDER BY id DESC LIMIT 1").fetchone()
        return Pip(row["id"], row["ts"], json.loads(row["terms_json"]), row["status"]) if row else None

    def issue_pip(self, terms: list[str]) -> Pip:
        ts = time.time()
        cur = self.db.execute("INSERT INTO pips(ts, terms_json) VALUES(?, ?)", (ts, json.dumps(terms)))
        self.db.commit()
        return Pip(int(cur.lastrowid or 0), ts, terms, "open")

    def resolve_pip(self, pip_id: int, status: str) -> None:
        self.db.execute("UPDATE pips SET status = ?, resolved_ts = ? WHERE id = ?", (status, time.time(), pip_id))
        self.db.commit()

    # --- history ---------------------------------------------------------
    def record(self, *, trigger: str, personality: str, question: str, transcript: str | None,
               evaluation: dict[str, Any], interrupted: bool, level_before: int, level_after: int,
               feedback: str, pip_issued: bool, barge_ins: int = 0) -> int:
        points = (evaluation["overall"] / PASS_POINTS_DIVISOR) if evaluation["passed"] else FAIL_POINTS
        if pip_issued:
            points += PIP_POINTS
        cur = self.db.execute(
            """INSERT INTO interrogations(ts, trigger, personality, question, transcript, word_count, jargon_ratio,
               overall, threshold, passed, evaluator, interrupted, level_before, level_after, scores_json,
               jargon_json, feedback, points, barge_ins) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (time.time(), trigger, personality, question, transcript, evaluation["word_count"],
             evaluation["jargon_ratio"], evaluation["overall"], evaluation["threshold"], int(evaluation["passed"]),
             evaluation["evaluator"], int(interrupted), level_before, level_after,
             json.dumps(evaluation["scores"]), json.dumps(evaluation["detected_jargon"]), feedback, points,
             barge_ins),
        )
        for term in set(evaluation["detected_jargon"]):
            self.db.execute("INSERT INTO buzzwords(term, count) VALUES(?, 1) "
                            "ON CONFLICT(term) DO UPDATE SET count = count + 1", (term,))
        streak = self.get("streak", 0) + 1 if evaluation["passed"] else 0
        self.set("streak", streak)
        self.set("best_streak", max(streak, self.get("best_streak", 0)))
        self.level = level_after
        self.db.commit()
        return int(cur.lastrowid or 0)

    @staticmethod
    def _row(r: sqlite3.Row) -> dict[str, Any]:
        d = dict(r)
        d["scores"] = json.loads(d.pop("scores_json"))
        d["detected_jargon"] = json.loads(d.pop("jargon_json"))
        d["passed"] = bool(d["passed"])
        return d

    def history(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT * FROM interrogations ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(r) for r in rows]

    def delete_interrogation(self, interrogation_id: int) -> bool:
        """Remove one interrogation and rebuild everything derived from it."""
        row = self.db.execute("SELECT * FROM interrogations WHERE id = ?", (interrogation_id,)).fetchone()
        if row is None:
            return False
        newest = self.db.execute("SELECT MAX(id) FROM interrogations").fetchone()[0]
        if row["points"] <= FAIL_POINTS + PIP_POINTS:
            # This failure issued a PIP (just before it was recorded); the PIP goes with it.
            self.db.execute("DELETE FROM pips WHERE id = (SELECT id FROM pips WHERE ts <= ? ORDER BY ts DESC LIMIT 1)",
                            (row["ts"],))
        self.db.execute("DELETE FROM interrogations WHERE id = ?", (interrogation_id,))
        self.set("barge_ins", max(0, int(self.get("barge_ins", 0)) - row["barge_ins"]))
        if row["id"] == newest:
            last = self.db.execute("SELECT level_after FROM interrogations ORDER BY id DESC LIMIT 1").fetchone()
            self.level = last["level_after"] if last else 0
        self._rebuild_derived()
        return True

    def reset(self) -> None:
        """Wipe history, reviews, PIPs and everything derived from them. Pause/kill switch state is kept."""
        for table in ("interrogations", "buzzwords", "pips", "reviews"):
            self.db.execute(f"DELETE FROM {table}")
        self.db.execute("DELETE FROM kv WHERE key IN ('streak', 'best_streak', 'barge_ins', 'escalation_level')")
        self.db.commit()

    def _rebuild_derived(self) -> None:
        """Recompute buzzword counts and streaks from the interrogations that remain."""
        self.db.execute("DELETE FROM buzzwords")
        streak = best = 0
        for r in self.db.execute("SELECT passed, jargon_json FROM interrogations ORDER BY id").fetchall():
            for term in set(json.loads(r["jargon_json"])):
                self.db.execute("INSERT INTO buzzwords(term, count) VALUES(?, 1) "
                                "ON CONFLICT(term) DO UPDATE SET count = count + 1", (term,))
            streak = streak + 1 if r["passed"] else 0
            best = max(best, streak)
        self.set("streak", streak)
        self.set("best_streak", best)
        self.db.commit()

    def between(self, start: float, end: float) -> list[dict[str, Any]]:
        """Interrogations with start <= ts < end, oldest first."""
        rows = self.db.execute("SELECT * FROM interrogations WHERE ts >= ? AND ts < ? ORDER BY ts",
                               (start, end)).fetchall()
        return [self._row(r) for r in rows]

    def points(self, before: float | None = None) -> float:
        if before is None:
            return float(self.db.execute("SELECT COALESCE(SUM(points), 0) FROM interrogations").fetchone()[0])
        return float(self.db.execute("SELECT COALESCE(SUM(points), 0) FROM interrogations WHERE ts < ?",
                                     (before,)).fetchone()[0])

    def pips_between(self, start: float, end: float) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM pips WHERE ts >= ? AND ts < ?", (start, end)).fetchone()[0])

    # --- weekly reviews --------------------------------------------------
    def add_review(self, *, period_start: float, period_end: float, personality: str, rating: int,
                   rating_name: str, summary: dict[str, Any], review: dict[str, Any], author: str,
                   delivered: bool, trigger: str = "scheduled") -> int:
        cur = self.db.execute(
            """INSERT INTO reviews(ts, period_start, period_end, personality, rating, rating_name, summary_json,
               review_json, author, trigger, delivered) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (time.time(), period_start, period_end, personality, rating, rating_name, json.dumps(summary),
             json.dumps(review), author, trigger, int(delivered)),
        )
        self.db.commit()
        return int(cur.lastrowid or 0)

    def mark_review_delivered(self, review_id: int) -> None:
        self.db.execute("UPDATE reviews SET delivered = 1 WHERE id = ?", (review_id,))
        self.db.commit()

    @staticmethod
    def _review_row(r: sqlite3.Row) -> dict[str, Any]:
        d = dict(r)
        d["summary"] = json.loads(d.pop("summary_json"))
        d["review"] = json.loads(d.pop("review_json"))
        d["delivered"] = bool(d["delivered"])
        return d

    def reviews(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT * FROM reviews ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._review_row(r) for r in rows]

    def review(self, review_id: int) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM reviews WHERE id = ?", (review_id,)).fetchone()
        return self._review_row(row) if row else None

    def delete_review(self, review_id: int) -> bool:
        cur = self.db.execute("DELETE FROM reviews WHERE id = ?", (review_id,))
        self.db.commit()
        return cur.rowcount > 0

    def has_scheduled_review_for(self, period_start: float) -> bool:
        """Manual reviews don't count: the Friday one still happens."""
        return self.db.execute("SELECT 1 FROM reviews WHERE period_start = ? AND trigger = 'scheduled' LIMIT 1",
                               (period_start,)).fetchone() is not None

    def stats(self) -> dict[str, Any]:
        agg = self.db.execute(
            """SELECT COUNT(*) total, COALESCE(SUM(passed), 0) passed,
                      COALESCE(AVG(CASE WHEN word_count > 0 THEN jargon_ratio END), 0) avg_density,
                      COALESCE(MAX(CASE WHEN passed = 1 THEN word_count END), 0) longest,
                      COALESCE(AVG(overall), 0) avg_overall
               FROM interrogations"""
        ).fetchone()
        top = self.db.execute("SELECT term, count FROM buzzwords ORDER BY count DESC, term LIMIT 10").fetchall()
        pips = self.db.execute("SELECT COUNT(*) FROM pips").fetchone()[0]
        points = self.points()
        rank_no, rank = rank_for(points)
        next_rank = RANKS[rank_no][0] if rank_no < len(RANKS) else None
        return {
            "total_interrogations": agg["total"],
            "successful_reports": agg["passed"],
            "failed_evaluations": agg["total"] - agg["passed"],
            "average_jargon_density": round(agg["avg_density"], 3),
            "average_score": round(agg["avg_overall"], 1),
            "longest_successful_monologue_words": agg["longest"],
            "top_buzzwords": [(r["term"], r["count"]) for r in top],
            "rank_number": rank_no,
            "rank": rank,
            "synergy_points": round(points, 1),
            "points_to_next_rank": None if next_rank is None else round(next_rank - points, 1),
            "consecutive_successes": self.get("streak", 0),
            "best_streak": self.get("best_streak", 0),
            "pips_issued": pips,
            "times_talked_over_management": int(self.get("barge_ins", 0)),
            "escalation_level": self.level,
            "open_pip": (p := self.open_pip()) and {"id": p.id, "terms": p.terms},
        }
