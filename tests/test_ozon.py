"""Справка Озон Банка. Только синтетические данные."""

import unittest

from phil.ozon_pdf import parse_pages
from phil.tbank_pdf import StatementParseError, Word


def w(x, top, text):
    out, pos = [], x
    for t in text.split():
        out.append(Word(t, pos, top))
        pos += len(t) * 5 + 3
    return out


def ozon_pages(ops, opening="1 000.00 ₽", closing="1 100.00 ₽", t_in="100.00 ₽", t_out="0.00 ₽"):
    p = []
    p += w(240, 30, "ООО «ОЗОН Банк», 123112, город Москва,")
    p += w(50, 150, "Справка о движении средств")
    p += w(50, 170, "№ Ф-2026-1 от «31» января 2026 года")
    p += w(50, 206, "Владелец: Иванов Иван Иванович")
    p += w(50, 227, "Номер лицевого счёта: № 40817810000000000003, открыт 01.01.2026")
    p += w(50, 270, "Период выписки: 01.01.2026 – 31.01.2026")
    p += w(50, 313, f"Входящий остаток: {opening}")
    p += w(55, 350, "Дата операции") + w(160, 350, "Документ") + w(225, 350, "Назначение платежа")
    p += w(347, 362, "Российские рубли") + w(452, 362, "Валюта")
    top = 385.0
    for date, time, doc, desc_lines, amount in ops:
        p += w(55, top, date) + w(103, top, time) + w(160, top, doc) + w(225, top, desc_lines[0])
        p += w(387, top, amount) + w(492, top, amount)
        for j, d in enumerate(desc_lines[1:], start=1):
            p += w(225, top + 14 * j, d)
        top += 14 * len(desc_lines) + 10
    p += w(50, top + 10, f"Итого зачислений за период: {t_in}")
    p += w(50, top + 24, f"Итого списаний за период: {t_out}")
    p += w(50, top + 38, f"Исходящий остаток: {closing}")
    return [p]


OPS = [("31.01.2026", "14:55:29", "1001", ["Выплата процентов по", "счету за январь."], "+ 100.00 ₽")]


class OzonTest(unittest.TestCase):
    def test_parse(self):
        s = parse_pages(ozon_pages(OPS), "f" * 64)
        self.assertEqual(s.account, "40817810000000000003")
        self.assertEqual(s.holder, "Иванов Иван Иванович")
        self.assertEqual((s.opening_minor, s.closing_minor), (100000, 110000))
        self.assertEqual(len(s.operations), 1)
        op = s.operations[0]
        self.assertEqual((op.amount_minor, op.operated_at, op.kind.kind), (10000, "2026-01-31T14:55", "interest"))
        self.assertEqual(op.description, "Выплата процентов по счету за январь.")
        self.assertEqual(s.check_totals(), [])

    def test_balance_mismatch(self):
        s = parse_pages(ozon_pages(OPS, closing="1 200.00 ₽"), "f" * 64)
        self.assertTrue(s.check_totals())

    def test_other_document(self):
        with self.assertRaises(StatementParseError):
            parse_pages([w(50, 150, "Справка о движении средств")])


if __name__ == "__main__":
    unittest.main()
