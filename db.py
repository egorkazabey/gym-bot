"""Хранилище на SQLite. Одна запись (entry) = одно сообщение, в ней подходы (sets)."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    raw_text   TEXT,
    created_at TEXT NOT NULL            -- локальное время ISO
);
CREATE TABLE IF NOT EXISTS sets (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id  INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    user_id   INTEGER NOT NULL,
    exercise  TEXT NOT NULL,
    weight    REAL NOT NULL,
    reps      INTEGER NOT NULL,
    day       TEXT NOT NULL             -- YYYY-MM-DD, локальная дата
);
CREATE INDEX IF NOT EXISTS idx_sets_user_ex ON sets(user_id, exercise, day);
CREATE TABLE IF NOT EXISTS aliases (
    user_id  INTEGER NOT NULL,
    alias    TEXT NOT NULL,
    exercise TEXT NOT NULL,
    PRIMARY KEY (user_id, alias)
);
"""


@dataclass
class Session:
    """Все подходы одного упражнения за один день."""
    day: date
    sets: list[tuple[float, int]]

    @property
    def top_weight(self) -> float:
        return max(w for w, _ in self.sets)

    @property
    def volume(self) -> float:
        return sum(w * r for w, r in self.sets)

    @property
    def total_reps(self) -> int:
        return sum(r for _, r in self.sets)

    @property
    def max_reps(self) -> int:
        return max(r for _, r in self.sets)


class DB:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # --- алиасы ---
    def resolve(self, user_id: int, name: str) -> str:
        with self._conn() as c:
            row = c.execute(
                "SELECT exercise FROM aliases WHERE user_id=? AND alias=?", (user_id, name)
            ).fetchone()
        return row[0] if row else name

    def set_alias(self, user_id: int, alias: str, exercise: str) -> int:
        """Создаёт алиас и переименовывает уже записанные подходы. Возвращает число перенесённых подходов."""
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO aliases(user_id, alias, exercise) VALUES (?,?,?)",
                (user_id, alias, exercise),
            )
            c.execute(
                "UPDATE aliases SET exercise=? WHERE user_id=? AND exercise=?",
                (exercise, user_id, alias),
            )
            cur = c.execute(
                "UPDATE sets SET exercise=? WHERE user_id=? AND exercise=?",
                (exercise, user_id, alias),
            )
            return cur.rowcount

    def list_aliases(self, user_id: int) -> list[tuple[str, str]]:
        with self._conn() as c:
            return c.execute(
                "SELECT alias, exercise FROM aliases WHERE user_id=? ORDER BY exercise, alias",
                (user_id,),
            ).fetchall()

    # --- запись ---
    def add_entry(
        self, user_id: int, raw: str, when: datetime, items: list[tuple[str, list[tuple[float, int]]]]
    ) -> int:
        day = when.date().isoformat()
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO entries(user_id, raw_text, created_at) VALUES (?,?,?)",
                (user_id, raw, when.isoformat(timespec="seconds")),
            )
            entry_id = cur.lastrowid
            c.executemany(
                "INSERT INTO sets(entry_id, user_id, exercise, weight, reps, day) VALUES (?,?,?,?,?,?)",
                [(entry_id, user_id, ex, w, r, day) for ex, sets in items for w, r in sets],
            )
        return entry_id

    def delete_entry(self, user_id: int, entry_id: int) -> bool:
        with self._conn() as c:
            cur = c.execute("DELETE FROM entries WHERE id=? AND user_id=?", (entry_id, user_id))
            return cur.rowcount > 0

    def last_entry(self, user_id: int) -> tuple[int, str] | None:
        with self._conn() as c:
            return c.execute(
                "SELECT id, raw_text FROM entries WHERE user_id=? ORDER BY id DESC LIMIT 1", (user_id,)
            ).fetchone()

    # --- чтение ---
    def sessions(self, user_id: int, exercise: str, before: date | None = None) -> list[Session]:
        q = "SELECT day, weight, reps FROM sets WHERE user_id=? AND exercise=?"
        args: list = [user_id, exercise]
        if before:
            q += " AND day < ?"
            args.append(before.isoformat())
        q += " ORDER BY day, id"
        with self._conn() as c:
            rows = c.execute(q, args).fetchall()
        out: list[Session] = []
        for d, w, r in rows:
            d = date.fromisoformat(d)
            if not out or out[-1].day != d:
                out.append(Session(d, []))
            out[-1].sets.append((w, r))
        return out

    def day_sets(self, user_id: int, start: date, end: date) -> list[tuple[str, str, float, int]]:
        with self._conn() as c:
            return c.execute(
                "SELECT day, exercise, weight, reps FROM sets WHERE user_id=? AND day BETWEEN ? AND ? "
                "ORDER BY day, id",
                (user_id, start.isoformat(), end.isoformat()),
            ).fetchall()

    def exercises(self, user_id: int) -> list[tuple[str, int, str]]:
        """(упражнение, число тренировок, последняя дата)"""
        with self._conn() as c:
            return c.execute(
                "SELECT exercise, COUNT(DISTINCT day), MAX(day) FROM sets WHERE user_id=? "
                "GROUP BY exercise ORDER BY MAX(day) DESC",
                (user_id,),
            ).fetchall()

    def all_sets(self, user_id: int) -> list[tuple[str, str, float, int]]:
        with self._conn() as c:
            return c.execute(
                "SELECT day, exercise, weight, reps FROM sets WHERE user_id=? ORDER BY day, id",
                (user_id,),
            ).fetchall()
