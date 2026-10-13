"""Сводка по реестру за период.

Каждая операция попадает в один из трёх потоков (см. labels.flow_of):
внешние приход и расход, перемещения между своими счетами и неуточнённые.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict

from .kinds import KIND_TITLES
from .labels import flow_of, label_map
from .tbank_pdf import fmt_minor


def _kind_table(title: str, data: dict[str, list[int]], sign: int) -> list[str]:
    if not data:
        return []
    out = ["", f"## {title}", "", "| Тип | Сумма | Операций |", "| --- | ---: | ---: |"]
    for kind, (amount, n) in sorted(data.items(), key=lambda kv: -abs(kv[1][0])):
        out.append(f"| {KIND_TITLES.get(kind, kind)} | {fmt_minor(sign * amount)} | {n} |")
    return out


def build_summary(conn: sqlite3.Connection, date_from: str | None = None, date_to: str | None = None,
                  top: int = 10) -> str:
    where, args = ["currency = 'RUB'"], []
    if date_from:
        where.append("substr(operated_at, 1, 10) >= ?")
        args.append(date_from)
    if date_to:
        where.append("substr(operated_at, 1, 10) <= ?")
        args.append(date_to)
    rows = conn.execute(f"SELECT * FROM operations WHERE {' AND '.join(where)}", args).fetchall()
    other_ccy = conn.execute("SELECT COUNT(*) FROM operations WHERE currency <> 'RUB'").fetchone()[0]
    imports = conn.execute(
        "SELECT MIN(period_from), MAX(period_to), COUNT(*) FROM imports"
    ).fetchone()
    if not rows:
        return "В реестре нет операций за выбранный период."

    labels = label_map(conn)
    ext_in: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    ext_out: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    unclear_in: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    unclear_out: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    own = {"in": 0, "out": 0, "n": 0}
    merchants: dict[str, list[int]] = defaultdict(lambda: [0, 0])

    for r in rows:
        flow, amount = flow_of(r, labels), r["amount_minor"]
        if flow == "own":
            own["in" if amount > 0 else "out"] += abs(amount)
            own["n"] += 1
            continue
        if flow == "external":
            bucket = ext_in if amount > 0 else ext_out
        else:
            bucket = unclear_in if amount > 0 else unclear_out
        bucket[r["kind"]][0] += amount
        bucket[r["kind"]][1] += 1
        if r["kind"] == "card_payment" and amount < 0:
            merchants[r["counterparty"] or "?"][0] += amount
            merchants[r["counterparty"] or "?"][1] += 1

    def total(d):
        return sum(v[0] for v in d.values())

    def count(d):
        return sum(v[1] for v in d.values())

    t_ext_in, t_ext_out = total(ext_in), -total(ext_out)
    t_un_in, t_un_out = total(unclear_in), -total(unclear_out)
    first = min(r["operated_at"] for r in rows)[:10]
    last = max(r["operated_at"] for r in rows)[:10]

    out = [
        "# Фил: сводка по личному счёту",
        "",
        f"Период операций: {first} — {last}. Операций: {len(rows)}.",
        f"Загружено выписок: {imports[2]}, покрытие {imports[0]} — {imports[1]}; "
        f"данные актуальны на {imports[1]}.",
        "",
        "| Поток | Пришло | Ушло | Операций |",
        "| --- | ---: | ---: | ---: |",
        f"| Внешние: доходы и расходы | {fmt_minor(t_ext_in)} | {fmt_minor(t_ext_out)} | "
        f"{count(ext_in) + count(ext_out)} |",
        f"| Между своими счетами | {fmt_minor(own['in'])} | {fmt_minor(own['out'])} | {own['n']} |",
        f"| Не уточнено | {fmt_minor(t_un_in)} | {fmt_minor(t_un_out)} | "
        f"{count(unclear_in) + count(unclear_out)} |",
        f"| **Всего по счёту** | {fmt_minor(t_ext_in + own['in'] + t_un_in)} | "
        f"{fmt_minor(t_ext_out + own['out'] + t_un_out)} | {len(rows)} |",
        "",
        "Перемещения между своими счетами не считаются ни доходом, ни расходом. "
        "Неуточнённые операции — переводы и пополнения, для которых ещё не отмечено, "
        "свой это счёт или чужой (`python3 -m phil counterparties`).",
    ]
    out += _kind_table("Внешние поступления", ext_in, 1)
    out += _kind_table("Внешние списания", ext_out, -1)
    out += _kind_table("Неуточнённые поступления", unclear_in, 1)
    out += _kind_table("Неуточнённые списания", unclear_out, -1)
    out += ["", f"## Крупнейшие продавцы по покупкам (топ-{top})", "",
            "| Продавец | Сумма | Покупок |", "| --- | ---: | ---: |"]
    for name, (amount, n) in sorted(merchants.items(), key=lambda kv: kv[1][0])[:top]:
        out.append(f"| {name} | {fmt_minor(-amount)} | {n} |")
    if other_ccy:
        out += ["", f"Операций в другой валюте: {other_ccy}; в эту сводку они не входят."]
    return "\n".join(out) + "\n"
