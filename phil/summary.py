"""Сводка по реестру за период.

Каждая операция попадает в один из трёх потоков (см. labels.flow_of):
внешние приход и расход, перемещения между своими счетами и неуточнённые.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict

from .kinds import KIND_TITLES
from .labels import flow_of, label_map
from .transfers import linked_ids
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
    accounts = conn.execute(
        """SELECT account, MAX(bank) AS bank, MIN(period_from) AS p_from, MAX(period_to) AS p_to, COUNT(*) AS n
           FROM imports GROUP BY account ORDER BY MAX(bank)"""
    ).fetchall()
    if not rows:
        return "В реестре нет операций за выбранный период."

    labels = label_map(conn)
    linked = linked_ids(conn)
    per_account: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    ext_in: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    ext_out: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    unclear_in: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    unclear_out: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    own = {"in": 0, "out": 0, "n": 0}
    merchants: dict[str, list[int]] = defaultdict(lambda: [0, 0])

    for r in rows:
        flow, amount = flow_of(r, labels, linked), r["amount_minor"]
        acc = per_account[r["account"]]
        acc[0 if amount > 0 else 1] += abs(amount)
        acc[2] += 1
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
        "# Фил: сводка по личным счетам",
        "",
        f"Запрошенный период: {date_from or 'с начала данных'} — {date_to or 'по конец данных'}. "
        f"Операции в нём: {first} — {last}, всего {len(rows)}.",
        "",
        "| Счёт | Выписки покрывают | Пришло | Ушло | Операций | Покрытие периода |",
        "| --- | --- | ---: | ---: | ---: | --- |",
    ]
    for a in accounts:
        got = per_account.get(a["account"], [0, 0, 0])
        gaps = []
        if date_from and a["p_from"] and a["p_from"] > date_from:
            gaps.append(f"нет данных до {a['p_from']}")
        if date_to and a["p_to"] and a["p_to"] < date_to:
            gaps.append(f"нет данных после {a['p_to']}")
        out.append(f"| {a['bank']} …{a['account'][-4:]} | {a['p_from']} — {a['p_to']} | {fmt_minor(got[0])} | "
                   f"{fmt_minor(got[1])} | {got[2]} | {'; '.join(gaps) or 'полное'} |")
    out += [
        "",
        "| Поток | Пришло | Ушло | Операций |",
        "| --- | ---: | ---: | ---: |",
        f"| Внешние: доходы и расходы | {fmt_minor(t_ext_in)} | {fmt_minor(t_ext_out)} | "
        f"{count(ext_in) + count(ext_out)} |",
        f"| Между своими счетами | {fmt_minor(own['in'])} | {fmt_minor(own['out'])} | {own['n']} |",
        f"| Не уточнено | {fmt_minor(t_un_in)} | {fmt_minor(t_un_out)} | "
        f"{count(unclear_in) + count(unclear_out)} |",
        f"| **Всего по счетам** | {fmt_minor(t_ext_in + own['in'] + t_un_in)} | "
        f"{fmt_minor(t_ext_out + own['out'] + t_un_out)} | {len(rows)} |",
        "",
        "Перемещения между своими счетами (в том числе найденные пары переводов между банками) "
        "не считаются ни доходом, ни расходом. Неуточнённые операции — переводы и пополнения, "
        "для которых ещё не отмечено, свой это счёт или чужой (`python3 -m phil counterparties`).",
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
