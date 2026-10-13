"""Реестр операций Фила в SQLite.

Первичные данные из выписки не изменяются. Повторная загрузка того же файла
отклоняется по SHA-256, повтор операции из пересекающейся выписки — по
отпечатку. Две одинаковые настоящие покупки в одну минуту различаются
порядковым номером среди одинаковых записей файла.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .kinds import classify
from .tbank_pdf import ParsedOperation, ParsedStatement

SCHEMA = """
CREATE TABLE IF NOT EXISTS imports (
    id              INTEGER PRIMARY KEY,
    file_sha256     TEXT NOT NULL UNIQUE,
    file_name       TEXT NOT NULL,
    imported_at     TEXT NOT NULL,
    source          TEXT NOT NULL,
    issue_number    TEXT,
    account         TEXT NOT NULL,
    period_from     TEXT,
    period_to       TEXT,
    total_in_minor  INTEGER,
    total_out_minor INTEGER,
    ops_in_file     INTEGER NOT NULL,
    ops_new         INTEGER NOT NULL,
    ops_known       INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS operations (
    id                INTEGER PRIMARY KEY,
    account           TEXT NOT NULL,
    fingerprint       TEXT NOT NULL,
    status            TEXT NOT NULL,
    operated_at       TEXT NOT NULL,
    debited_at        TEXT,
    amount_minor      INTEGER NOT NULL,
    currency          TEXT NOT NULL,
    card_amount_minor INTEGER NOT NULL,
    card_currency     TEXT NOT NULL,
    description       TEXT NOT NULL,
    card_last4        TEXT,
    kind              TEXT NOT NULL,
    counterparty      TEXT,
    counterparty_ref  TEXT,
    first_import_id   INTEGER NOT NULL REFERENCES imports(id),
    source_page       INTEGER NOT NULL,
    source_ordinal    INTEGER NOT NULL,
    UNIQUE (account, fingerprint)
);
CREATE INDEX IF NOT EXISTS operations_operated_at ON operations(account, operated_at);
"""


class ImportRejected(Exception):
    """Импорт не выполнен; в реестре ничего не изменилось."""


@dataclass
class ImportResult:
    import_id: int
    ops_in_file: int
    ops_new: int
    ops_known: int


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def _base_key(account: str, op: ParsedOperation) -> str:
    # Дата списания не входит в ключ: ожидающая операция позже приходит проведённой.
    return "|".join([
        account, op.operated_at, str(op.amount_minor), op.currency,
        str(op.card_amount_minor), op.card_currency, op.description, op.card_last4 or "",
    ])


def fingerprints(account: str, ops: list[ParsedOperation]) -> list[str]:
    """Отпечаток = хэш полей операции + номер среди полностью одинаковых в файле."""
    seen: Counter[str] = Counter()
    result = []
    for op in ops:
        key = _base_key(account, op)
        seen[key] += 1
        result.append(hashlib.sha256(f"{key}#{seen[key]}".encode()).hexdigest())
    return result


def import_statement(conn: sqlite3.Connection, stmt: ParsedStatement, file_name: str,
                     source: str = "tbank_pdf_funds_movement") -> ImportResult:
    if not stmt.account:
        raise ImportRejected("В справке не найден номер счёта")
    problems = stmt.check_totals()
    if problems:
        raise ImportRejected("Суммы операций не сходятся с итогами справки: " + "; ".join(problems))
    if conn.execute("SELECT 1 FROM imports WHERE file_sha256 = ?", (stmt.file_sha256,)).fetchone():
        raise ImportRejected("Этот файл уже загружен")

    fps = fingerprints(stmt.account, stmt.operations)
    with conn:  # одна транзакция: при сбое не остаётся частичного импорта
        cur = conn.execute(
            """INSERT INTO imports (file_sha256, file_name, imported_at, source, issue_number, account,
                   period_from, period_to, total_in_minor, total_out_minor, ops_in_file, ops_new, ops_known)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0)""",
            (stmt.file_sha256, file_name, datetime.now(timezone.utc).isoformat(timespec="seconds"),
             source, stmt.issue_number, stmt.account, stmt.period_from, stmt.period_to,
             stmt.total_in_minor, stmt.total_out_minor, len(stmt.operations)),
        )
        import_id = cur.lastrowid
        new = 0
        for op, fp in zip(stmt.operations, fps):
            k = classify(op.description)
            cur = conn.execute(
                """INSERT OR IGNORE INTO operations (account, fingerprint, status, operated_at, debited_at,
                       amount_minor, currency, card_amount_minor, card_currency, description, card_last4,
                       kind, counterparty, counterparty_ref, first_import_id, source_page, source_ordinal)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (stmt.account, fp, "posted" if op.debited_at else "pending", op.operated_at, op.debited_at,
                 op.amount_minor, op.currency, op.card_amount_minor, op.card_currency, op.description,
                 op.card_last4, k.kind, k.counterparty, k.counterparty_ref, import_id, op.page, op.ordinal),
            )
            new += cur.rowcount
            if cur.rowcount == 0 and op.debited_at:
                # Ранее ожидавшая операция проведена: дополняем, не создавая повтор.
                conn.execute(
                    """UPDATE operations SET status = 'posted', debited_at = ?
                       WHERE account = ? AND fingerprint = ? AND status = 'pending'""",
                    (op.debited_at, stmt.account, fp),
                )
        known = len(stmt.operations) - new
        conn.execute("UPDATE imports SET ops_new = ?, ops_known = ? WHERE id = ?", (new, known, import_id))
    return ImportResult(import_id, len(stmt.operations), new, known)
