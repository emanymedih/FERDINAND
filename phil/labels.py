"""Разметка получателей и отправителей переводов.

Ответ пользователя относится к реквизиту (договор, телефон, счёт, карта) и
применяется ко всем прошлым и будущим операциям с ним. Первичные операции не
меняются; прежние отметки сохраняются в истории.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

LABELS = {
    "own": "свой счёт владельца выписки: перемещение денег, не доход и не расход",
    "external": "другой человек или организация: настоящий приход или расход",
}

# Типы, которые по самой записи являются перемещением между своими счетами.
INHERENTLY_OWN = {"transfer_self", "savings_out", "savings_in"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS counterparty_labels (
    account TEXT NOT NULL,
    ref     TEXT NOT NULL,
    label   TEXT NOT NULL CHECK (label IN ('own', 'external')),
    note    TEXT,
    set_at  TEXT NOT NULL,
    PRIMARY KEY (account, ref)
);
CREATE TABLE IF NOT EXISTS counterparty_label_history (
    id      INTEGER PRIMARY KEY,
    account TEXT NOT NULL,
    ref     TEXT NOT NULL,
    label   TEXT,
    note    TEXT,
    set_at  TEXT NOT NULL
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def resolve_ref(conn: sqlite3.Connection, ref: str) -> tuple[str, str]:
    """Находит (счёт выписки, полный реквизит) по полному значению или его окончанию."""
    rows = conn.execute(
        "SELECT DISTINCT account, counterparty_ref FROM operations "
        "WHERE counterparty_ref = ? OR counterparty_ref LIKE ?",
        (ref, f"%{ref}"),
    ).fetchall()
    exact = [r for r in rows if r[1] == ref]
    rows = exact or rows
    if not rows:
        raise ValueError(f"В реестре нет операций с реквизитом {ref}")
    if len(rows) > 1:
        raise ValueError(f"Окончание {ref} подходит к нескольким реквизитам: "
                         + ", ".join(r[1] for r in rows))
    return rows[0][0], rows[0][1]


def set_label(conn: sqlite3.Connection, ref: str, label: str, note: str | None = None) -> str:
    if label not in LABELS:
        raise ValueError(f"Отметка должна быть одной из: {', '.join(LABELS)}")
    ensure_schema(conn)
    account, full_ref = resolve_ref(conn, ref)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with conn:
        conn.execute(
            "INSERT INTO counterparty_label_history (account, ref, label, note, set_at) "
            "SELECT account, ref, label, note, set_at FROM counterparty_labels WHERE account = ? AND ref = ?",
            (account, full_ref),
        )
        conn.execute(
            "INSERT OR REPLACE INTO counterparty_labels (account, ref, label, note, set_at) VALUES (?, ?, ?, ?, ?)",
            (account, full_ref, label, note, now),
        )
    return full_ref


def label_map(conn: sqlite3.Connection) -> dict[tuple[str, str], str]:
    ensure_schema(conn)
    return {(a, r): l for a, r, l in conn.execute("SELECT account, ref, label FROM counterparty_labels")}


def flow_of(row: sqlite3.Row, labels: dict[tuple[str, str], str],
            linked: set[int] | frozenset = frozenset()) -> str:
    """'own' — перемещение между своими счетами, 'external' — подтверждённо внешнее,
    'unclear' — смысл не установлен. `linked` — операции из сопоставленных пар переводов."""
    from .kinds import NEEDS_MEANING

    if row["kind"] in INHERENTLY_OWN or row["id"] in linked:
        return "own"
    ref = row["counterparty_ref"]
    if ref:
        label = labels.get((row["account"], ref))
        if label:
            return label
    if row["kind"] in NEEDS_MEANING or row["kind"] == "unknown":
        return "unclear"
    return "external"


def counterparties(conn: sqlite3.Connection) -> list[dict]:
    labels = label_map(conn)
    rows = conn.execute(
        """SELECT account, counterparty_ref AS ref, GROUP_CONCAT(DISTINCT kind) AS kinds,
                  SUM(CASE WHEN amount_minor < 0 THEN -amount_minor ELSE 0 END) AS out_minor,
                  SUM(CASE WHEN amount_minor > 0 THEN amount_minor ELSE 0 END) AS in_minor,
                  COUNT(*) AS n, MIN(substr(operated_at,1,10)) AS first, MAX(substr(operated_at,1,10)) AS last
           FROM operations WHERE counterparty_ref IS NOT NULL
           GROUP BY account, counterparty_ref"""
    ).fetchall()
    result = []
    for r in rows:
        kinds = set(r["kinds"].split(","))
        result.append({
            "account": r["account"], "ref": r["ref"], "kinds": kinds,
            "out_minor": r["out_minor"], "in_minor": r["in_minor"], "n": r["n"],
            "first": r["first"], "last": r["last"],
            "label": "own" if kinds <= INHERENTLY_OWN else labels.get((r["account"], r["ref"])),
            "both_ways": r["out_minor"] > 0 and r["in_minor"] > 0,
        })
    result.sort(key=lambda c: -(c["out_minor"] + c["in_minor"]))
    return result
