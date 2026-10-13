"""Вопросы владельцу по неуточнённым операциям.

Фил не спрашивает о каждой операции. Он группирует неуточнённые операции по
реквизиту (или по типу, если реквизита нет), сортирует по сумме и отмечает
регулярность: одна и та же сумма в разных месяцах — признак обязательного
платежа, о котором стоит спросить в первую очередь. Мелочь ниже порога не
попадает в вопросы.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict

from .kinds import KIND_TITLES
from .labels import flow_of, label_map
from .tbank_pdf import fmt_minor
from .transfers import linked_ids

MIN_TOTAL_MINOR = 10_000_00  # 10 000 ₽ за период


def build_questions(conn: sqlite3.Connection, date_from: str | None = None, date_to: str | None = None,
                    limit: int = 7, min_total_minor: int = MIN_TOTAL_MINOR) -> list[dict]:
    where, args = ["1=1"], []
    if date_from:
        where.append("substr(operated_at, 1, 10) >= ?")
        args.append(date_from)
    if date_to:
        where.append("substr(operated_at, 1, 10) <= ?")
        args.append(date_to)
    rows = conn.execute(f"SELECT * FROM operations WHERE {' AND '.join(where)}", args).fetchall()
    labels, linked = label_map(conn), linked_ids(conn)

    groups: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        if flow_of(r, labels, linked) != "unclear":
            continue
        key = ("ref", r["counterparty_ref"]) if r["counterparty_ref"] else ("kind", r["kind"])
        groups[key].append(r)

    result = []
    for (by, value), ops in groups.items():
        out = -sum(o["amount_minor"] for o in ops if o["amount_minor"] < 0)
        inc = sum(o["amount_minor"] for o in ops if o["amount_minor"] > 0)
        if out + inc < min_total_minor:
            continue
        months_by_amount: dict[int, set[str]] = defaultdict(set)
        for o in ops:
            months_by_amount[o["amount_minor"]].add(o["operated_at"][:7])
        regular = max((len(m) for m in months_by_amount.values()), default=0) >= 2
        kinds = sorted({o["kind"] for o in ops})
        result.append({
            "subject": value if by == "ref" else KIND_TITLES.get(value, value),
            "by": by, "kinds": kinds, "out_minor": out, "in_minor": inc, "n": len(ops),
            "first": min(o["operated_at"] for o in ops)[:10], "last": max(o["operated_at"] for o in ops)[:10],
            "regular": regular,
        })
    # Регулярные платежи важнее разовых; внутри — по сумме.
    result.sort(key=lambda q: (not q["regular"], -(q["out_minor"] + q["in_minor"])))
    return result[:limit]


def format_questions(questions: list[dict]) -> str:
    if not questions:
        return "Вопросов нет: все заметные операции уточнены."
    lines = ["# Фил: вопросы по неуточнённым операциям", ""]
    for i, q in enumerate(questions, start=1):
        parts = []
        if q["out_minor"]:
            parts.append(f"ушло {fmt_minor(q['out_minor'])}")
        if q["in_minor"]:
            parts.append(f"пришло {fmt_minor(q['in_minor'])}")
        kinds = ", ".join(KIND_TITLES.get(k, k).lower() for k in q["kinds"])
        regular = " **Регулярно, одной суммой.**" if q["regular"] else ""
        what = f"Реквизит {q['subject']}" if q["by"] == "ref" else q["subject"]
        lines.append(f"{i}. {what} ({kinds}): {', '.join(parts)}, операций {q['n']}, "
                     f"{q['first']} — {q['last']}.{regular} Свой счёт или внешний платёж, и что это?")
    lines += ["", "Ответ записывается командой `label` (реквизит) или `label-kind` (тип без реквизита)."]
    return "\n".join(lines) + "\n"
