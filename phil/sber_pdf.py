"""Разбор PDF «Выписка по платёжному счёту» СберБанка.

Каждая операция занимает две строки и больше:
  1) дата, время (МСК), категория, сумма в валюте счёта, остаток после операции;
  2) дата обработки, код авторизации, описание (может продолжаться ниже),
     иногда сумма в валюте операции.
Поступления помечены «+», списания идут без знака. В выписке есть остатки
на начало и конец периода и остаток после каждой операции; по ним проверяется,
что ни одна операция не потеряна и не прочитана с ошибкой.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .kinds import Kind
from .tbank_pdf import (
    DATE_RE, TIME_RE, ParsedOperation, ParsedStatement, StatementParseError, Word,
    file_sha256, group_lines, iso_date, line_text,
)

PERIOD_RE = re.compile(r"За период (\d{2}\.\d{2}\.\d{4})\s*[—–-]\s*(\d{2}\.\d{2}\.\d{4})")
AUTH_RE = re.compile(r"^\d{4,8}$")
CARD_RE = re.compile(r"Операция по карте \*{2,4}(\d{4})")

# Границы колонок по заголовку «ДАТА ОПЕРАЦИИ … КАТЕГОРИЯ … СУММА … ОСТАТОК».
CURRENCIES = {"Российский рубль": "RUB", "Доллар США": "USD", "Евро": "EUR", "Китайский юань": "CNY"}


def parse_plain_amount(text: str) -> int:
    """'+1 234,50' -> 123450; '19,00' -> 1900 (списание без знака возвращается как есть)."""
    raw = text.replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise StatementParseError(f"Не распознана сумма: {text!r}") from exc
    minor = value * 100
    if minor != minor.to_integral_value():
        raise StatementParseError(f"Сумма точнее копейки: {text!r}")
    return int(minor)


def classify_sber(category: str, description: str, amount_minor: int) -> Kind:
    d, c = description, category
    if re.match(r"^(Заработная плата|Аванс по заработной плате|Зарплата)", d):
        return Kind("salary", d.split(".")[0], None)
    if CARD_RE.search(d) and amount_minor < 0:
        merchant = d.split(". Операция по карте")[0].strip()
        if c.startswith("Выдача наличных"):
            return Kind("cash_out", merchant, None)
        return Kind("card_payment", merchant, None)
    if c.startswith("Внесение наличных"):
        return Kind("cash_in", None, None)
    if c.startswith("Выдача наличных"):
        return Kind("cash_out", None, None)
    if c.startswith("Перевод СБП"):
        bank = d.split(".")[0].strip() or None
        return Kind("transfer_out_sbp" if amount_minor < 0 else "transfer_in_sbp", bank, None)
    if c.startswith("Перевод") or "Перевод" in d:
        return Kind("transfer_out_other" if amount_minor < 0 else "transfer_in_other", d.split(".")[0], None)
    if CARD_RE.search(d) and amount_minor > 0:
        return Kind("refund", d.split(". Операция по карте")[0].strip(), None)
    return Kind("unknown", None, None)


def _columns(header: list[Word]) -> tuple[float, float, float, float]:
    """Начала колонок: время/код, категория/описание, сумма, остаток."""
    xs = {w.text: w.x0 for w in header}
    try:
        return xs["(МСК)"] - 10, xs["КАТЕГОРИЯ"], xs["СУММА"] - 15, xs["ОСТАТОК"]
    except KeyError as exc:
        raise StatementParseError("Заголовок таблицы Сбера не совпадает с известным форматом: "
                                  + line_text(header)) from exc


def parse_pages(pages: list[list[Word]], sha256: str = "") -> ParsedStatement:
    all_lines = [(n, line) for n, words in enumerate(pages, start=1) for line in group_lines(words)]
    texts = [line_text(l) for _, l in all_lines]
    full = "\n".join(texts)
    if "Выписка по платёжному счёту" not in full and "Выписка по платежному счету" not in full:
        raise StatementParseError("Это не выписка по платёжному счёту СберБанка")

    stmt = ParsedStatement(
        file_sha256=sha256, issue_number=None, issue_date=None, holder=None, contract=None,
        account=None, period_from=None, period_to=None, total_in_minor=None, total_out_minor=None,
        source="sber_pdf_payment_account", bank="Сбер",
    )
    m = PERIOD_RE.search(full)
    if m:
        stmt.period_from, stmt.period_to = iso_date(m.group(1)), iso_date(m.group(2))
    currency = "RUB"
    balances: list[int] = []

    for i, (page_no, line) in enumerate(all_lines):
        t = texts[i]
        if t.startswith("Владелец счёта") and i + 1 < len(texts):
            stmt.holder = texts[i + 1].split("Остаток")[0].strip()
        if "Номер счёта" in t:
            digits = re.sub(r"\D", "", t.split("Номер счёта", 1)[1])
            if len(digits) >= 20:
                stmt.account = digits[:20]
        for name, code in CURRENCIES.items():
            if t.startswith("Валюта") and name in t:
                currency = code
        bm = re.search(r"Остаток на \d{2}\.\d{2}\.\d{4}\s+([+-]?[\d\s]+,\d{2})", t)
        if bm:
            balances.append(parse_plain_amount(bm.group(1)))
        pm = re.search(r"^Пополнение\s+([\d\s]+,\d{2})$", t)
        if pm:
            stmt.total_in_minor = parse_plain_amount(pm.group(1))
        sm = re.search(r"^Списание\s+([\d\s]+,\d{2})$", t)
        if sm:
            stmt.total_out_minor = parse_plain_amount(sm.group(1))
        dm = re.search(r"Дата формирования документа\s+(\d{2}\.\d{2}\.\d{4})", t)
        if dm:
            stmt.issue_date = iso_date(dm.group(1))
    if len(balances) >= 2:
        stmt.opening_minor, stmt.closing_minor = balances[0], balances[1]
    else:
        stmt.warnings.append("Не найдены остатки на начало и конец периода")

    cols = None
    in_table = False
    blocks: list[dict] = []
    current: dict | None = None
    for i, (page_no, line) in enumerate(all_lines):
        t = texts[i]
        if t.startswith("ДАТА ОПЕРАЦИИ"):
            cols, in_table, current = _columns(line), True, None
            continue
        if not in_table:
            continue
        if (t.startswith("Дата формирования") or t.startswith("ПАО Сбербанк")
                or t.startswith("Выписка по платёжному счёту") or t.startswith("Продолжение на")):
            in_table, current = False, None
            continue
        if t.startswith("Дата обработки") or t.startswith("и код авторизации") or t == "*":
            continue
        x_time, x_desc, x_amount, x_balance = cols
        first, second = line[0], line[1] if len(line) > 1 else None
        desc = " ".join(w.text for w in line if x_desc - 2 <= w.x0 < x_amount)
        amount = "".join(w.text for w in line if x_amount <= w.x0 < x_balance - 2)
        balance = "".join(w.text for w in line if w.x0 >= x_balance - 2)
        is_date = DATE_RE.match(first.text) and first.x0 < x_time
        if is_date and second is not None and TIME_RE.match(second.text) and amount:
            current = {"page": page_no, "date": first.text, "time": second.text, "category": desc,
                       "amount": amount, "balance": balance, "desc": [], "debited": None, "op_amount": None}
            blocks.append(current)
        elif is_date and second is not None and AUTH_RE.match(second.text) and current and current["debited"] is None:
            current["debited"] = first.text
            current["desc"].append(desc)
            if amount:
                current["op_amount"] = amount
        elif current is not None and desc and not is_date:
            current["desc"].append(desc)
        elif t.strip():
            stmt.warnings.append(f"Стр. {page_no}: строка не распознана: {t}")

    for ordinal, b in enumerate(blocks, start=1):
        raw = b["amount"]
        value = parse_plain_amount(raw.lstrip("+"))
        amount = value if raw.startswith("+") else -value
        description = re.sub(r"\s+", " ", " ".join(b["desc"])).strip()
        card = CARD_RE.search(description)
        op = ParsedOperation(
            page=b["page"], ordinal=ordinal,
            operated_date=b["date"], operated_time=b["time"],
            debited_date=b["debited"], debited_time=None,
            amount_minor=amount, currency=currency,
            card_amount_minor=amount, card_currency=currency,
            description=description, card_last4=card.group(1) if card else None,
            category=b["category"],
            balance_after_minor=parse_plain_amount(b["balance"]) if b["balance"] else None,
        )
        if b["op_amount"]:
            stmt.warnings.append(f"Операция {op.operated_at}: сумма в валюте операции {b['op_amount']} "
                                 "сохранена только в предупреждении")
        op.kind = classify_sber(op.category or "", description, amount)
        stmt.operations.append(op)
    return stmt


def parse_pdf(path: str | Path) -> ParsedStatement:
    import pdfplumber

    path = Path(path)
    with pdfplumber.open(path) as pdf:
        pages = [[Word(w["text"], w["x0"], w["top"]) for w in p.extract_words()] for p in pdf.pages]
    return parse_pages(pages, file_sha256(path))
