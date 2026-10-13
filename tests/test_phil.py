"""Тесты Фила на синтетических данных. Реальные выписки в тестах не используются.

Запуск из корня репозитория: python3 -m unittest
"""

import sqlite3
import unittest

from phil.kinds import classify
from phil.registry import ImportRejected, connect, import_statement
from phil.summary import build_summary
from phil.tbank_pdf import StatementParseError, Word, parse_money, parse_pages

# Начала колонок как в настоящей справке (pt).
X = {"operated": 56.0, "debited": 126.0, "amount": 199.0, "card_amount": 294.0,
     "description": 389.0, "card": 499.0}
ACCOUNT = "40817810000000000001"  # синтетический номер


def words_at(x: float, top: float, text: str) -> list[Word]:
    out, pos = [], x
    for t in text.split():
        out.append(Word(t, pos, top))
        pos += len(t) * 5 + 3
    return out


def header(top: float) -> list[Word]:
    w = []
    w += words_at(X["operated"], top, "Дата и время")
    w += words_at(X["debited"], top, "Дата")
    w += words_at(X["amount"], top, "Сумма в валюте")
    w += words_at(X["card_amount"], top, "Сумма операции")
    w += words_at(X["description"], top, "Описание")
    w += words_at(X["card"], top, "Номер")
    w += words_at(X["operated"], top + 12, "операции")
    w += words_at(X["debited"], top + 12, "списания")
    w += words_at(X["amount"], top + 12, "операции")
    w += words_at(X["card_amount"], top + 12, "в валюте карты")
    w += words_at(X["description"], top + 12, "операции")
    w += words_at(X["card"], top + 12, "карты")
    return w


def op_row(top: float, date: str, time: str, amount: str, desc_lines: list[str],
           card: str = "1234", debited: tuple[str, str] | None = None) -> list[Word]:
    deb_date, deb_time = debited or (date, time)
    w = []
    w += words_at(X["operated"], top, date)
    w += words_at(X["debited"], top, deb_date)
    w += words_at(X["amount"], top, amount)
    w += words_at(X["card_amount"], top, amount)
    w += words_at(X["description"], top, desc_lines[0])
    w += words_at(X["card"], top, card)
    w += words_at(X["operated"], top + 12, time)
    w += words_at(X["debited"], top + 12, deb_time)
    for i, line in enumerate(desc_lines[1:], start=1):
        if i == 1:
            w += words_at(X["description"], top + 12, line)
        else:
            w += words_at(X["description"], top + 12 * i, line)
    return w


def statement_pages(ops: list[dict], total_in: str, total_out: str,
                    period=("01.01.2026", "31.01.2026"), split_after: int | None = None) -> list[list[Word]]:
    p1 = []
    p1 += words_at(56, 120, "Справка о движении средств")
    p1 += words_at(56, 140, "Исх. № test001 31.01.2026")
    p1 += words_at(56, 232, "Номер договора: 1000000001")
    p1 += words_at(56, 250, f"Номер лицевого счета: {ACCOUNT}")
    p1 += words_at(56, 270, f"Движение средств за период с {period[0]} по {period[1]}")
    p1 += header(298)
    pages, cur, top = [p1], p1, 330.0
    for i, op in enumerate(ops):
        if split_after is not None and i == split_after:
            cur = header(30)
            cur += words_at(530, 803, "1")  # номер страницы — колонтитул
            pages.append(cur)
            top = 62.0
        cur += op_row(top, **op)
        top += 12 * (max(2, len(op["desc_lines"]) + 0)) + 8
    cur += words_at(56, top + 10, f"Пополнения: {total_in}")
    cur += words_at(56, top + 25, f"Расходы: {total_out}")
    return pages


OPS = [
    dict(date="20.01.2026", time="12:00", amount="+50 000.00 ₽", desc_lines=["Пополнение. Система", "быстрых платежей"]),
    dict(date="15.01.2026", time="10:00", amount="-1 250.50 ₽", desc_lines=["Оплата в SHOP", "Moscow RUS"]),
    dict(date="15.01.2026", time="10:00", amount="-1 250.50 ₽", desc_lines=["Оплата в SHOP", "Moscow RUS"]),
    dict(date="10.01.2026", time="09:00", amount="-15 000.00 ₽",
         desc_lines=["Внешний банковский", "перевод счёт", "40802810000000000002,", "Филиал Банка"]),
    dict(date="05.01.2026", time="23:15", amount="-99.00 ₽", desc_lines=["Плата за обслуживание"], card="—"),
]


def parse(ops=OPS, total_in="50 000,00 ₽", total_out="17 600,00 ₽", sha="a" * 64, **kw):
    return parse_pages(statement_pages(ops, total_in, total_out, **kw), sha)


class MoneyTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_money("-10 000.00 ₽"), (-1000000, "RUB"))
        self.assertEqual(parse_money("+1 275.00 ₽"), (127500, "RUB"))
        self.assertEqual(parse_money("5 882 141,61 ₽"), (588214161, "RUB"))
        self.assertEqual(parse_money("-12.30 $"), (-1230, "USD"))

    def test_rejects_unknown(self):
        with self.assertRaises(StatementParseError):
            parse_money("100.00")
        with self.assertRaises(StatementParseError):
            parse_money("1.005 ₽")


class ParseTest(unittest.TestCase):
    def test_structure_and_totals(self):
        s = parse()
        self.assertEqual(s.account, ACCOUNT)
        self.assertEqual((s.period_from, s.period_to), ("2026-01-01", "2026-01-31"))
        self.assertEqual(len(s.operations), 5)
        self.assertEqual(s.check_totals(), [])
        self.assertEqual(s.operations[0].description, "Пополнение. Система быстрых платежей")
        self.assertEqual(s.operations[3].description,
                         "Внешний банковский перевод счёт 40802810000000000002, Филиал Банка")
        self.assertIsNone(s.operations[4].card_last4)
        self.assertEqual(s.operations[1].operated_at, "2026-01-15T10:00")

    def test_page_break_repeats_header(self):
        s = parse(split_after=2)
        self.assertEqual(len(s.operations), 5)
        self.assertEqual(s.check_totals(), [])
        self.assertEqual(s.operations[2].page, 2)

    def test_totals_mismatch_detected(self):
        s = parse(total_out="17 700,00 ₽")
        self.assertTrue(s.check_totals())

    def test_not_a_statement(self):
        with self.assertRaises(StatementParseError):
            parse_pages([words_at(56, 100, "Какой-то другой документ")])


class KindTest(unittest.TestCase):
    def test_kinds(self):
        cases = {
            "Оплата в FUNPAY": ("card_payment", "FUNPAY", None),
            "Внутренний перевод на договор 5000000001": ("transfer_out_internal", None, "5000000001"),
            "Внешний перевод по номеру телефона +79000000000": ("transfer_out_phone", None, "+79000000000"),
            "Внутрибанковский перевод с договора Т-Банк": ("transfer_in_internal", None, None),
            "Внутренний перевод на вклад 3000000000": ("savings_out", None, "3000000000"),
            "Плата за оповещения об операциях": ("bank_fee", "оповещения об операциях", None),
            "Что-то новое": ("unknown", None, None),
        }
        for desc, (kind, cp, ref) in cases.items():
            k = classify(desc)
            self.assertEqual((k.kind, k.counterparty, k.counterparty_ref), (kind, cp, ref), desc)


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.conn = connect(":memory:")

    def count(self):
        return self.conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0]

    def test_import_keeps_identical_real_purchases(self):
        res = import_statement(self.conn, parse(), "jan.pdf")
        self.assertEqual((res.ops_new, res.ops_known), (5, 0))
        self.assertEqual(self.count(), 5)

    def test_same_file_rejected(self):
        import_statement(self.conn, parse(), "jan.pdf")
        with self.assertRaises(ImportRejected):
            import_statement(self.conn, parse(), "jan-copy.pdf")
        self.assertEqual(self.count(), 5)

    def test_overlapping_statement_adds_only_new(self):
        import_statement(self.conn, parse(), "jan.pdf")
        newer = [dict(date="02.02.2026", time="08:00", amount="-500.00 ₽", desc_lines=["Оплата в CAFE"])] + OPS
        s = parse(newer, total_out="18 100,00 ₽", sha="b" * 64, period=("01.01.2026", "02.02.2026"))
        res = import_statement(self.conn, s, "jan-feb.pdf")
        self.assertEqual((res.ops_new, res.ops_known), (1, 5))
        self.assertEqual(self.count(), 6)

    def test_mismatched_totals_rejected_without_changes(self):
        with self.assertRaises(ImportRejected):
            import_statement(self.conn, parse(total_out="1,00 ₽"), "bad.pdf")
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM imports").fetchone()[0], 0)

    def test_summary_numbers(self):
        import_statement(self.conn, parse(), "jan.pdf")
        text = build_summary(self.conn)
        self.assertIn("**Поступило:** 50 000,00 ₽", text)
        self.assertIn("**Списано:** 17 600,00 ₽", text)
        self.assertIn("**Разница:** 32 400,00 ₽", text)
        self.assertIn("| SHOP Moscow RUS | 2 501,00 ₽ | 2 |", text)


if __name__ == "__main__":
    unittest.main()
