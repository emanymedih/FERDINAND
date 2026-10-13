"""Сопоставление переводов между своими счетами в разных банках.

Пара — это списание с одного счёта и поступление на другой счёт того же
владельца: равная сумма в одной валюте и время с разницей не больше
MAX_DELTA_MINUTES. Пара создаётся только однозначная: у списания ровно один
подходящий кандидат, и у поступления тоже. Неоднозначные случаи не
угадываются, а возвращаются для ручного разбора. Связанные операции сводка
считает перемещением между своими счетами.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

MAX_DELTA_MINUTES = 10

OUT_KINDS = {
    "transfer_out_internal", "transfer_out_phone", "transfer_out_bank", "transfer_out_card",
    "transfer_out_sbp", "transfer_out_other",
}
IN_KINDS = {"topup", "transfer_in_internal", "transfer_in_sbp", "transfer_in_other", "bank_transfer_in"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS transfer_links (
    out_op_id     INTEGER PRIMARY KEY REFERENCES operations(id),
    in_op_id      INTEGER NOT NULL UNIQUE REFERENCES operations(id),
    delta_minutes INTEGER NOT NULL,
    method        TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _minutes(ts: str) -> int:
    fmt = "%Y-%m-%dT%H:%M" if "T" in ts else "%Y-%m-%d"
    return int(datetime.strptime(ts, fmt).timestamp() // 60)


def _holders(conn: sqlite3.Connection) -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    for account, holder in conn.execute("SELECT account, holder FROM imports"):
        if holder:
            result[account] = " ".join(holder.lower().replace("ё", "е").split())
        else:
            result.setdefault(account, None)
    return result


def linked_ids(conn: sqlite3.Connection) -> set[int]:
    ensure_schema(conn)
    ids: set[int] = set()
    for out_id, in_id in conn.execute("SELECT out_op_id, in_op_id FROM transfer_links"):
        ids.update((out_id, in_id))
    return ids


def match_transfers(conn: sqlite3.Connection) -> tuple[int, list[str]]:
    """Создаёт однозначные пары. Возвращает (число новых пар, описания неоднозначных)."""
    ensure_schema(conn)
    holders = _holders(conn)
    taken = linked_ids(conn)

    def free(kinds: set[str], sign: str) -> list[sqlite3.Row]:
        marks = ",".join("?" * len(kinds))
        rows = conn.execute(
            f"SELECT id, account, operated_at, amount_minor, currency, description FROM operations "
            f"WHERE kind IN ({marks}) AND amount_minor {sign} 0 AND instr(operated_at, 'T') > 0",
            sorted(kinds),
        ).fetchall()
        return [r for r in rows if r["id"] not in taken]

    outs, ins = free(OUT_KINDS, "<"), free(IN_KINDS, ">")

    def same_owner(a: str, b: str) -> bool:
        ha, hb = holders.get(a), holders.get(b)
        return ha is not None and ha == hb

    def candidates(op: sqlite3.Row, pool: list[sqlite3.Row]) -> list[sqlite3.Row]:
        t = _minutes(op["operated_at"])
        return [
            p for p in pool
            if p["account"] != op["account"] and p["currency"] == op["currency"]
            and p["amount_minor"] == -op["amount_minor"] and same_owner(p["account"], op["account"])
            and abs(_minutes(p["operated_at"]) - t) <= MAX_DELTA_MINUTES
        ]

    new, ambiguous = 0, []
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with conn:
        for out in outs:
            cand = candidates(out, ins)
            if len(cand) == 1 and len(candidates(cand[0], outs)) == 1:
                inc = cand[0]
                conn.execute(
                    "INSERT INTO transfer_links (out_op_id, in_op_id, delta_minutes, method, created_at) "
                    "VALUES (?, ?, ?, 'amount_and_time', ?)",
                    (out["id"], inc["id"], abs(_minutes(inc["operated_at"]) - _minutes(out["operated_at"])), now),
                )
                new += 1
            elif len(cand) > 1:
                ambiguous.append(
                    f"{out['operated_at']} {out['description']}: подходят {len(cand)} поступления"
                )
    return new, ambiguous


def list_links(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    ensure_schema(conn)
    return conn.execute(
        """SELECT o.operated_at AS out_at, o.account AS out_account, o.description AS out_desc,
                  i.operated_at AS in_at, i.account AS in_account, i.description AS in_desc,
                  i.amount_minor AS amount, l.delta_minutes
           FROM transfer_links l JOIN operations o ON o.id = l.out_op_id JOIN operations i ON i.id = l.in_op_id
           ORDER BY o.operated_at"""
    ).fetchall()
