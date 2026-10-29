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
    points REAL NOT NULL DEFAULT 0
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
"""


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
               feedback: str, pip_issued: bool) -> int:
        points = (evaluation["overall"] / PASS_POINTS_DIVISOR) if evaluation["passed"] else FAIL_POINTS
        if pip_issued:
            points += PIP_POINTS
        cur = self.db.execute(
            """INSERT INTO interrogations(ts, trigger, personality, question, transcript, word_count, jargon_ratio,
               overall, threshold, passed, evaluator, interrupted, level_before, level_after, scores_json,
               jargon_json, feedback, points) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (time.time(), trigger, personality, question, transcript, evaluation["word_count"],
             evaluation["jargon_ratio"], evaluation["overall"], evaluation["threshold"], int(evaluation["passed"]),
             evaluation["evaluator"], int(interrupted), level_before, level_after,
             json.dumps(evaluation["scores"]), json.dumps(evaluation["detected_jargon"]), feedback, points),
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

    def history(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT * FROM interrogations ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["scores"] = json.loads(d.pop("scores_json"))
            d["detected_jargon"] = json.loads(d.pop("jargon_json"))
            d["passed"] = bool(d["passed"])
            out.append(d)
        return out

    def points(self) -> float:
        return float(self.db.execute("SELECT COALESCE(SUM(points), 0) FROM interrogations").fetchone()[0])

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
            "escalation_level": self.level,
            "open_pip": (p := self.open_pip()) and {"id": p.id, "terms": p.terms},
        }
