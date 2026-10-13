"""Сводка по реестру за период: поступления и списания по типам операций."""

from __future__ import annotations

import sqlite3
from collections import defaultdict

from .kinds import KIND_TITLES, NEEDS_MEANING
from .tbank_pdf import fmt_minor


def build_summary(conn: sqlite3.Connection, date_from: str | None = None, date_to: str | None = None,
                  top: int = 10) -> str:
    where, args = ["currency = 'RUB'"], []
    if date_from:
        where.append("substr(operated_at, 1, 10) >= ?")
        args.append(date_from)
    if date_to:
        where.append("substr(operated_at, 1, 10) <= ?")
        args.append(date_to)
    cond = " AND ".join(where)

    rows = conn.execute(f"SELECT * FROM operations WHERE {cond}", args).fetchall()
    other_ccy = conn.execute(
        "SELECT COUNT(*) FROM operations WHERE currency <> 'RUB'"
    ).fetchone()[0]
    imports = conn.execute(
        "SELECT MIN(period_from), MAX(period_to), MAX(imported_at), COUNT(*) FROM imports"
    ).fetchone()
    if not rows:
        return "В реестре нет операций за выбранный период."

    by_kind_in: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    by_kind_out: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    merchants: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    needs = [0, 0]
    for r in rows:
        bucket = by_kind_in if r["amount_minor"] > 0 else by_kind_out
        bucket[r["kind"]][0] += r["amount_minor"]
        bucket[r["kind"]][1] += 1
        if r["kind"] == "card_payment" and r["amount_minor"] < 0:
            merchants[r["counterparty"] or "?"][0] += r["amount_minor"]
            merchants[r["counterparty"] or "?"][1] += 1
        if r["kind"] in NEEDS_MEANING or r["kind"] == "unknown":
            needs[0] += 1
            needs[1] += abs(r["amount_minor"])

    total_in = sum(v[0] for v in by_kind_in.values())
    total_out = sum(v[0] for v in by_kind_out.values())
    first = min(r["operated_at"] for r in rows)[:10]
    last = max(r["operated_at"] for r in rows)[:10]

    out = [
        "# Фил: сводка по личному счёту",
        "",
        f"Период операций: {first} — {last}. Операций: {len(rows)}.",
        f"Загружено выписок: {imports[3]}, покрытие {imports[0]} — {imports[1]}; "
        f"данные актуальны на {imports[1]}.",
        "",
        f"**Поступило:** {fmt_minor(total_in)}  ",
        f"**Списано:** {fmt_minor(-total_out)}  ",
        f"**Разница:** {fmt_minor(total_in + total_out)}",
        "",
        "Это движение денег по счёту, а не доходы и расходы: переводы между своими",
        "счетами и копилками пока не отделены от настоящих трат.",
        "",
        "## Поступления по типам",
        "",
        "| Тип | Сумма | Операций |",
        "| --- | ---: | ---: |",
    ]
    for kind, (amount, n) in sorted(by_kind_in.items(), key=lambda kv: -kv[1][0]):
        out.append(f"| {KIND_TITLES.get(kind, kind)} | {fmt_minor(amount)} | {n} |")
    out += ["", "## Списания по типам", "", "| Тип | Сумма | Операций |", "| --- | ---: | ---: |"]
    for kind, (amount, n) in sorted(by_kind_out.items(), key=lambda kv: kv[1][0]):
        out.append(f"| {KIND_TITLES.get(kind, kind)} | {fmt_minor(-amount)} | {n} |")
    out += ["", f"## Крупнейшие продавцы по покупкам (топ-{top})", "",
            "| Продавец | Сумма | Покупок |", "| --- | ---: | ---: |"]
    for name, (amount, n) in sorted(merchants.items(), key=lambda kv: kv[1][0])[:top]:
        out.append(f"| {name} | {fmt_minor(-amount)} | {n} |")
    out += [
        "",
        "## Требует уточнения",
        "",
        f"{needs[0]} операций на {fmt_minor(needs[1])} — переводы, пополнения и наличные,",
        "у которых по выписке не видно смысла: перемещение между своими счетами, долг,",
        "оплата услуги или подарок. Их разбор — следующий шаг.",
    ]
    if other_ccy:
        out += ["", f"Операций в другой валюте: {other_ccy}; в эту сводку они не входят."]
    return "\n".join(out) + "\n"
