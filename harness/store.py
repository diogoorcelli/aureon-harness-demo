"""Estado persistente em SQLite: leads, mensagens, agenda, orçamentos, eventos.

A tabela `appointments` faz o papel do Google Calendar (FakeCalendar) e
`processed_events` faz a deduplicação de webhooks.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    session_id TEXT PRIMARY KEY,
    name TEXT,
    temperature TEXT NOT NULL DEFAULT 'cold',
    last_user_ts TEXT,
    last_agent_ts TEXT,
    followups_sent INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS appointments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    day TEXT NOT NULL,
    time TEXT NOT NULL,
    procedure TEXT NOT NULL,
    created_ts TEXT NOT NULL,
    UNIQUE(day, time)
);
CREATE TABLE IF NOT EXISTS quotes (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    total REAL NOT NULL,
    status TEXT NOT NULL,
    created_ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS processed_events (
    event_id TEXT PRIMARY KEY
);
"""

TEMP_ORDER = {"cold": 0, "warm": 1, "hot": 2}


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str | Path = ":memory:"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # --- leads -------------------------------------------------------------
    def ensure_session(self, sid: str, now: datetime) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO leads(session_id, last_user_ts) VALUES (?, ?)",
            (sid, _iso(now)),
        )
        self.db.commit()

    def get_lead(self, sid: str) -> dict:
        row = self.db.execute("SELECT * FROM leads WHERE session_id=?", (sid,)).fetchone()
        return dict(row) if row else {}

    def set_name(self, sid: str, name: str) -> None:
        self.db.execute("UPDATE leads SET name=? WHERE session_id=?", (name, sid))
        self.db.commit()

    def set_temperature(self, sid: str, temp: str) -> str:
        """A temperatura só sobe dentro de uma sessão (cold -> warm -> hot)."""
        current = self.get_lead(sid).get("temperature", "cold")
        if TEMP_ORDER[temp] > TEMP_ORDER[current]:
            self.db.execute("UPDATE leads SET temperature=? WHERE session_id=?", (temp, sid))
            self.db.commit()
            return temp
        return current

    # --- mensagens ---------------------------------------------------------
    def add_message(self, sid: str, role: str, content: str, now: datetime) -> None:
        self.db.execute(
            "INSERT INTO messages(session_id, role, content, ts) VALUES (?,?,?,?)",
            (sid, role, content, _iso(now)),
        )
        if role == "user":  # lead respondeu: zera o ciclo de follow-up
            self.db.execute(
                "UPDATE leads SET last_user_ts=?, followups_sent=0 WHERE session_id=?",
                (_iso(now), sid),
            )
        else:
            self.db.execute(
                "UPDATE leads SET last_agent_ts=? WHERE session_id=?", (_iso(now), sid)
            )
        self.db.commit()

    def recent_messages(self, sid: str, n: int) -> list[dict]:
        rows = self.db.execute(
            "SELECT role, content FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?",
            (sid, n),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def older_user_messages(self, sid: str, skip_last: int) -> list[str]:
        rows = self.db.execute(
            "SELECT content FROM messages WHERE session_id=? AND role='user' "
            "ORDER BY id DESC LIMIT -1 OFFSET ?",
            (sid, skip_last),
        ).fetchall()
        return [r["content"] for r in reversed(rows)]

    def count_messages(self, sid: str) -> int:
        return self.db.execute(
            "SELECT COUNT(*) c FROM messages WHERE session_id=?", (sid,)
        ).fetchone()["c"]

    # --- agenda ------------------------------------------------------------
    def booked_times(self, day: str) -> set[str]:
        rows = self.db.execute("SELECT time FROM appointments WHERE day=?", (day,)).fetchall()
        return {r["time"] for r in rows}

    def book(self, sid: str, day: str, time: str, procedure: str, now: datetime):
        """Retorna (status, row). Idempotente: mesma sessão + mesmo horário não duplica.

        Insere primeiro e deixa o UNIQUE(day, time) decidir: não há janela entre
        checar e inserir em que outra conexão possa ocupar o horário.
        """
        try:
            self.db.execute(
                "INSERT INTO appointments(session_id, day, time, procedure, created_ts) "
                "VALUES (?,?,?,?,?)",
                (sid, day, time, procedure, _iso(now)),
            )
            self.db.commit()
            inserted = True
        except sqlite3.IntegrityError:
            self.db.rollback()  # libera a transação aberta pelo INSERT recusado
            inserted = False
        row = self.db.execute(
            "SELECT * FROM appointments WHERE day=? AND time=?", (day, time)
        ).fetchone()
        if inserted:
            return "confirmed", dict(row)
        return ("already_booked" if row["session_id"] == sid else "slot_taken"), dict(row)

    def count_appointments(self, sid: str | None = None) -> int:
        if sid:
            q, a = "SELECT COUNT(*) c FROM appointments WHERE session_id=?", (sid,)
        else:
            q, a = "SELECT COUNT(*) c FROM appointments", ()
        return self.db.execute(q, a).fetchone()["c"]

    # --- orçamentos --------------------------------------------------------
    def add_quote(self, qid: str, sid: str, total: float, status: str, now: datetime) -> bool:
        """Grava ou atualiza o orçamento da própria sessão. False se o id pertence a outra sessão."""
        cur = self.db.execute(
            "INSERT INTO quotes(id, session_id, total, status, created_ts) VALUES (?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET total=excluded.total, status=excluded.status, "
            "created_ts=excluded.created_ts WHERE quotes.session_id = excluded.session_id",
            (qid, sid, total, status, _iso(now)),
        )
        self.db.commit()
        return cur.rowcount == 1

    def delete_quote(self, qid: str, sid: str) -> None:
        self.db.execute("DELETE FROM quotes WHERE id=? AND session_id=?", (qid, sid))
        self.db.commit()

    # --- webhooks ----------------------------------------------------------
    def mark_event_processed(self, event_id: str) -> bool:
        """True se o evento é novo; False se já foi processado (deduplicação)."""
        try:
            self.db.execute("INSERT INTO processed_events(event_id) VALUES (?)", (event_id,))
            self.db.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    # --- follow-up ---------------------------------------------------------
    def all_leads(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM leads").fetchall()]

    def record_followup(self, sid: str, now: datetime) -> None:
        self.db.execute(
            "UPDATE leads SET followups_sent=followups_sent+1, last_agent_ts=? "
            "WHERE session_id=?",
            (_iso(now), sid),
        )
        self.db.commit()
