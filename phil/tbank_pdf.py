"""Разбор PDF «Справка о движении средств» Т-Банка (личный счёт).

Разбор опирается на координаты слов, а не на текстовую вёрстку: колонки
определяются по строке заголовка таблицы на каждой странице. Логика разбора
отделена от чтения PDF, поэтому тесты подают синтетические слова без файла.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

DATE_RE = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")
TIME_RE = re.compile(r"^\d{2}:\d{2}$")
PERIOD_RE = re.compile(r"период\s+с\s+(\d{2}\.\d{2}\.\d{4})\s+по\s+(\d{2}\.\d{2}\.\d{4})")
ACCOUNT_RE = re.compile(r"Номер лицевого сч[её]та:\s*(\d{20})")
CONTRACT_RE = re.compile(r"Номер договора:\s*(\d+)")
ISSUED_RE = re.compile(r"Исх\.\s*№\s*(\S+)\s+(\d{2}\.\d{2}\.\d{4})")

CURRENCY_SYMBOLS = {"₽": "RUB", "$": "USD", "€": "EUR", "¥": "CNY", "£": "GBP"}

# Порядок колонок таблицы и слово заголовка, с которого колонка начинается.
COLUMNS = ("operated", "debited", "amount", "card_amount", "description", "card")
LINE_TOLERANCE = 2.5  # pt: слова с близким `top` относятся к одной строке


class StatementParseError(ValueError):
    """Файл не похож на справку о движении средств или нарушена его структура."""


@dataclass(frozen=True)
class Word:
    text: str
    x0: float
    top: float


@dataclass
class ParsedOperation:
    page: int
    ordinal: int  # порядковый номер строки в файле, начиная с 1
    operated_date: str  # ДД.ММ.ГГГГ как в источнике
    operated_time: str | None
    debited_date: str | None
    debited_time: str | None
    amount_minor: int
    currency: str
    card_amount_minor: int
    card_currency: str
    description: str
    card_last4: str | None
    category: str | None = None  # категория банка, если она есть в источнике
    balance_after_minor: int | None = None  # остаток после операции, если он есть
    kind: object | None = None  # phil.kinds.Kind, если тип определил адаптер

    @property
    def operated_at(self) -> str:
        """ISO-дата и время операции (часовой пояс источником не указан)."""
        return _iso(self.operated_date, self.operated_time)

    @property
    def debited_at(self) -> str | None:
        return _iso(self.debited_date, self.debited_time) if self.debited_date else None


@dataclass
class ParsedStatement:
    file_sha256: str
    issue_number: str | None
    issue_date: str | None
    holder: str | None
    contract: str | None
    account: str | None
    period_from: str | None
    period_to: str | None
    total_in_minor: int | None
    total_out_minor: int | None
    operations: list[ParsedOperation] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    source: str = "tbank_pdf_funds_movement"
    bank: str = "Т-Банк"
    opening_minor: int | None = None
    closing_minor: int | None = None

    def check_totals(self) -> list[str]:
        """Сверяет операции с итогами и остатками документа. Пустой список — сошлось."""
        problems = self._check_balances()
        got_in = sum(o.amount_minor for o in self.operations if o.amount_minor > 0)
        got_out = -sum(o.amount_minor for o in self.operations if o.amount_minor < 0)
        if self.total_in_minor is None or self.total_out_minor is None:
            problems.append("В справке не найдены итоги «Пополнения» и «Расходы»")
            return problems
        if got_in != self.total_in_minor:
            problems.append(
                f"Пополнения: по операциям {fmt_minor(got_in)}, в справке {fmt_minor(self.total_in_minor)}"
            )
        if got_out != self.total_out_minor:
            problems.append(
                f"Расходы: по операциям {fmt_minor(got_out)}, в справке {fmt_minor(self.total_out_minor)}"
            )
        return problems

    def _check_balances(self) -> list[str]:
        """Остатки: начальный + операции = конечный; остаток после каждой операции.

        Операции идут от новых к старым, как в выписках банков."""
        problems: list[str] = []
        if self.opening_minor is None or self.closing_minor is None:
            return problems
        total = sum(o.amount_minor for o in self.operations)
        if self.opening_minor + total != self.closing_minor:
            problems.append(
                f"Остатки: {fmt_minor(self.opening_minor)} + операции {fmt_minor(total)} "
                f"≠ {fmt_minor(self.closing_minor)}"
            )
        balance = self.opening_minor
        for op in reversed(self.operations):
            balance += op.amount_minor
            if op.balance_after_minor is not None and op.balance_after_minor != balance:
                problems.append(
                    f"Стр. {op.page}, операция {op.operated_at} {fmt_minor(op.amount_minor)}: "
                    f"остаток в выписке {fmt_minor(op.balance_after_minor)}, расчётный {fmt_minor(balance)}"
                )
                break
        return problems


def _iso(date: str, time: str | None) -> str:
    d, m, y = date.split(".")
    return f"{y}-{m}-{d}" + (f"T{time}" if time else "")


def iso_date(date: str | None) -> str | None:
    return _iso(date, None) if date else None


def fmt_minor(value: int) -> str:
    sign = "-" if value < 0 else ""
    rub, kop = divmod(abs(value), 100)
    return f"{sign}{rub:,}".replace(",", " ") + f",{kop:02d} ₽"


def parse_money(text: str) -> tuple[int, str]:
    """'-10 000.00 ₽' -> (-1000000, 'RUB'). Деньги только через Decimal."""
    raw = text.replace(" ", " ").strip()
    currency = None
    for symbol, code in CURRENCY_SYMBOLS.items():
        if symbol in raw:
            currency = code
            raw = raw.replace(symbol, "")
    if currency is None:
        raise StatementParseError(f"Не распознана валюта суммы: {text!r}")
    raw = raw.replace(" ", "").replace(",", ".")
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise StatementParseError(f"Не распознана сумма: {text!r}") from exc
    minor = value * 100
    if minor != minor.to_integral_value():
        raise StatementParseError(f"Сумма точнее копейки: {text!r}")
    return int(minor), currency


def group_lines(words: list[Word]) -> list[list[Word]]:
    lines: list[list[Word]] = []
    for w in sorted(words, key=lambda w: (w.top, w.x0)):
        if lines and abs(lines[-1][0].top - w.top) <= LINE_TOLERANCE:
            lines[-1].append(w)
        else:
            lines.append([w])
    return [sorted(line, key=lambda w: w.x0) for line in lines]


def line_text(line: list[Word]) -> str:
    return " ".join(w.text for w in line)


def _column_starts(header: list[Word]) -> list[float]:
    """Начала колонок по строке заголовка: Дата, Дата, Сумма, Сумма, Описание, Номер."""
    expected = ["Дата", "Дата", "Сумма", "Сумма", "Описание", "Номер"]
    starts, i = [], 0
    for w in header:
        if i < len(expected) and w.text == expected[i]:
            starts.append(w.x0)
            i += 1
    if len(starts) != len(expected):
        raise StatementParseError(
            "Заголовок таблицы не совпадает с известным форматом: " + line_text(header)
        )
    return starts


def _split_columns(line: list[Word], starts: list[float]) -> dict[str, str]:
    cells: dict[str, list[str]] = {c: [] for c in COLUMNS}
    for w in line:
        idx = 0
        for j, s in enumerate(starts):
            if w.x0 >= s - 2:
                idx = j
        cells[COLUMNS[idx]].append(w.text)
    return {c: " ".join(v) for c, v in cells.items()}


def parse_pages(pages: list[list[Word]], file_sha256: str = "") -> ParsedStatement:
    all_lines = [(n, line) for n, words in enumerate(pages, start=1) for line in group_lines(words)]
    full_text = "\n".join(line_text(line) for _, line in all_lines)

    def first(regex: re.Pattern, group: int = 1) -> str | None:
        m = regex.search(full_text)
        return m.group(group) if m else None

    if "Справка о движении средств" not in full_text:
        raise StatementParseError("Это не «Справка о движении средств» Т-Банка")

    stmt = ParsedStatement(
        file_sha256=file_sha256,
        issue_number=first(ISSUED_RE, 1),
        issue_date=iso_date(first(ISSUED_RE, 2)),
        holder=None,
        contract=first(CONTRACT_RE),
        account=first(ACCOUNT_RE),
        period_from=iso_date(first(PERIOD_RE, 1)),
        period_to=iso_date(first(PERIOD_RE, 2)),
        total_in_minor=None,
        total_out_minor=None,
    )

    starts: list[float] | None = None
    in_table = False
    header_tail = False
    current: dict | None = None
    blocks: list[dict] = []

    for page_no, line in all_lines:
        text = line_text(line)
        if text.startswith("Дата и время") and "Описание" in text:
            starts = _column_starts(line)
            in_table = True
            header_tail = True
            continue
        if in_table and header_tail:
            header_tail = False
            if text.startswith("операции"):  # вторая строка заголовка
                continue
        if text.startswith("Пополнения:"):
            stmt.total_in_minor = parse_money(text.split(":", 1)[1])[0]
            in_table = False
            continue
        if text.startswith("Расходы:"):
            stmt.total_out_minor = parse_money(text.split(":", 1)[1])[0]
            continue
        if not in_table or starts is None:
            continue
        if text.startswith("АО «ТБанк»") or text.startswith("БИК ") or re.fullmatch(r"\d{1,3}", text):
            continue  # колонтитул страницы
        cells = _split_columns(line, starts)
        if DATE_RE.match(cells["operated"]):
            current = {"page": page_no, "cells": {k: [v] if v else [] for k, v in cells.items()}}
            blocks.append(current)
        elif current is not None:
            for k, v in cells.items():
                if v:
                    current["cells"][k].append(v)
        else:
            stmt.warnings.append(f"Стр. {page_no}: строка вне операции пропущена: {text}")

    if stmt.total_in_minor is None or stmt.total_out_minor is None:
        stmt.warnings.append("Итоги справки не найдены: файл мог быть обрезан")

    for ordinal, block in enumerate(blocks, start=1):
        c = block["cells"]
        op_date, op_time = _date_time(c["operated"], "операции", block["page"])
        deb_date, deb_time = (_date_time(c["debited"], "списания", block["page"])
                              if c["debited"] else (None, None))
        amount, currency = parse_money(" ".join(c["amount"]))
        card_amount, card_currency = parse_money(" ".join(c["card_amount"]))
        card = " ".join(c["card"]).strip()
        stmt.operations.append(ParsedOperation(
            page=block["page"],
            ordinal=ordinal,
            operated_date=op_date,
            operated_time=op_time,
            debited_date=deb_date,
            debited_time=deb_time,
            amount_minor=amount,
            currency=currency,
            card_amount_minor=card_amount,
            card_currency=card_currency,
            description=re.sub(r"\s+", " ", " ".join(c["description"])).strip(),
            card_last4=card if re.fullmatch(r"\d{4}", card) else None,
        ))
    return stmt


def _date_time(parts: list[str], what: str, page: int) -> tuple[str, str | None]:
    tokens = " ".join(parts).split()
    if not tokens or not DATE_RE.match(tokens[0]):
        raise StatementParseError(f"Стр. {page}: не распознана дата {what}: {parts}")
    time = tokens[1] if len(tokens) > 1 and TIME_RE.match(tokens[1]) else None
    return tokens[0], time


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_pdf(path: str | Path) -> ParsedStatement:
    import pdfplumber  # тяжёлая зависимость нужна только для настоящего файла

    path = Path(path)
    with pdfplumber.open(path) as pdf:
        pages = [
            [Word(w["text"], w["x0"], w["top"]) for w in page.extract_words()]
            for page in pdf.pages
        ]
    stmt = parse_pages(pages, file_sha256(path))
    # ФИО — первая строка после номера справки; хранится только для сверки владельца.
    lines = [line_text(l) for l in group_lines(pages[0])] if pages else []
    for i, text in enumerate(lines):
        if text.startswith("Исх.") and i + 1 < len(lines):
            stmt.holder = lines[i + 1]
            break
    return stmt
