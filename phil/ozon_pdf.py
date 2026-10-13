"""Разбор PDF «Справка о движении средств» Озон Банка.

Строка операции: дата и время, номер документа, назначение платежа
(продолжается ниже), сумма в рублях и сумма в валюте счёта. В справке есть
входящий и исходящий остатки и итоги зачислений и списаний; по ним
проверяется полнота.
"""

from __future__ import annotations

import re
from pathlib import Path

from .kinds import Kind
from .tbank_pdf import (
    DATE_RE, ParsedOperation, ParsedStatement, StatementParseError, Word,
    file_sha256, group_lines, iso_date, line_text, parse_money,
)

TIME_S_RE = re.compile(r"^\d{2}:\d{2}:\d{2}$")
MONEY = r"([+-]?\s*[\d\s]+[.,]\d{2}\s*₽)"


def classify_ozon(description: str, amount_minor: int) -> Kind:
    d = description
    if d.startswith("Выплата процентов"):
        return Kind("interest", "Озон Банк", None)
    if re.search(r"СБП|Система быстрых платежей", d):
        return Kind("transfer_in_sbp" if amount_minor > 0 else "transfer_out_sbp", None, None)
    if "Перевод" in d or "перевод" in d:
        return Kind("transfer_in_other" if amount_minor > 0 else "transfer_out_other", None, None)
    if re.search(r"Оплата|Покупка", d) and amount_minor < 0:
        return Kind("card_payment", d, None)
    return Kind("unknown", None, None)


def parse_pages(pages: list[list[Word]], sha256: str = "") -> ParsedStatement:
    all_lines = [(n, line) for n, ws in enumerate(pages, start=1) for line in group_lines(ws)]
    texts = [line_text(l) for _, l in all_lines]
    full = "\n".join(texts)
    if "ОЗОН Банк" not in full or "Справка о движении средств" not in full:
        raise StatementParseError("Это не справка о движении средств Озон Банка")

    stmt = ParsedStatement(
        file_sha256=sha256, issue_number=None, issue_date=None, holder=None, contract=None,
        account=None, period_from=None, period_to=None, total_in_minor=None, total_out_minor=None,
        source="ozon_pdf_funds_movement", bank="Озон Банк",
    )

    def find(rx: str, flags=0):
        m = re.search(rx, full, flags)
        return m.groups() if m else None

    if g := find(r"№\s*(\S+)\s+от"):
        stmt.issue_number = g[0]
    if g := find(r"Владелец:\s*(.+)"):
        stmt.holder = g[0].strip()
    if g := find(r"Номер лицевого сч[её]та:\s*№?\s*(\d{20})"):
        stmt.account = g[0]
    if g := find(r"Период выписки:\s*(\d{2}\.\d{2}\.\d{4})\s*[–—-]\s*(\d{2}\.\d{2}\.\d{4})"):
        stmt.period_from, stmt.period_to = iso_date(g[0]), iso_date(g[1])
    if g := find(r"формирования документа:\s*(\d{2}\.\d{2}\.\d{4})"):
        stmt.issue_date = iso_date(g[0])
    if g := find(r"Входящий остаток:\s*" + MONEY):
        stmt.opening_minor = parse_money(g[0])[0]
    if g := find(r"Исходящий остаток:\s*" + MONEY):
        stmt.closing_minor = parse_money(g[0])[0]
    if g := find(r"Итого зачислений за период:\s*" + MONEY):
        stmt.total_in_minor = parse_money(g[0])[0]
    if g := find(r"Итого списаний за период:\s*" + MONEY):
        stmt.total_out_minor = abs(parse_money(g[0])[0])
    if stmt.opening_minor is None or stmt.closing_minor is None:
        stmt.warnings.append("Не найдены входящий и исходящий остатки")

    cols = None
    in_table = False
    blocks: list[dict] = []
    current = None
    for i, (page_no, line) in enumerate(all_lines):
        t = texts[i]
        if t.startswith("Дата операции") and "Назначение" in t:
            xs = {w.text: w.x0 for w in line}
            cols = {"doc": xs["Документ"], "desc": xs["Назначение"]}
            in_table = True
            continue
        if in_table and "Российские" in t and "Валюта" in t:
            xs = {w.text: w.x0 for w in line}
            cols["rub"], cols["ccy"] = xs["Российские"] - 5, xs["Валюта"] - 5
            continue
        if t.startswith("Итого зачислений"):
            in_table, current = False, None
            continue
        if not in_table or cols is None or "rub" not in cols:
            continue
        first = line[0]
        if DATE_RE.match(first.text) and len(line) > 1 and TIME_S_RE.match(line[1].text):
            current = {
                "page": page_no, "date": first.text, "time": line[1].text[:5],
                "doc": " ".join(w.text for w in line if cols["doc"] - 2 <= w.x0 < cols["desc"] - 2),
                "desc": [" ".join(w.text for w in line if cols["desc"] - 2 <= w.x0 < cols["rub"])],
                "rub": " ".join(w.text for w in line if cols["rub"] <= w.x0 < cols["ccy"]),
            }
            blocks.append(current)
        elif current is not None:
            current["desc"].append(" ".join(w.text for w in line if w.x0 < cols["rub"]))

    for ordinal, b in enumerate(blocks, start=1):
        amount, currency = parse_money(b["rub"])
        desc = re.sub(r"\s+", " ", " ".join(b["desc"])).strip()
        op = ParsedOperation(
            page=b["page"], ordinal=ordinal, operated_date=b["date"], operated_time=b["time"],
            debited_date=b["date"], debited_time=b["time"], amount_minor=amount, currency=currency,
            card_amount_minor=amount, card_currency=currency, description=desc, card_last4=None,
            category=f"Документ {b['doc']}" if b["doc"] else None,
        )
        op.kind = classify_ozon(desc, amount)
        stmt.operations.append(op)
    # В справке операции от новых к старым или единственная; проверка остатков порядок не требует.
    return stmt


def parse_pdf(path: str | Path) -> ParsedStatement:
    import pdfplumber

    path = Path(path)
    with pdfplumber.open(path) as pdf:
        pages = [[Word(w["text"], w["x0"], w["top"]) for w in p.extract_words()] for p in pdf.pages]
    return parse_pages(pages, file_sha256(path))
