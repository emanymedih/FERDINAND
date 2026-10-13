"""Сбер и сопоставление переводов между банками. Только синтетические данные."""

import sqlite3
import unittest

from phil.registry import ImportRejected, connect, import_statement
from phil.sber_pdf import parse_pages as parse_sber
from phil.summary import build_summary
from phil.tbank_pdf import StatementParseError, Word
from phil.transfers import list_links, match_transfers
from tests.test_phil import parse as parse_tbank

HOLDER = "Иванов Иван Иванович"  # синтетический владелец
SBER_ACCOUNT = "40817 810 0 0000 0000002"


def w(x: float, top: float, text: str) -> list[Word]:
    out, pos = [], x
    for t in text.split():
        out.append(Word(t, pos, top))
        pos += len(t) * 5 + 3
    return out


def sber_pages(ops: list[tuple], opening: str, closing: str, holder: str = HOLDER,
               totals: tuple[str, str] | None = None) -> list[list[Word]]:
    """ops: (дата, время, категория, сумма '+1 000,00' или '500,00', остаток, описание)."""
    inc = sum(float(o[3].replace(" ", "").replace(",", ".")) for o in ops if o[3].startswith("+"))
    out = sum(float(o[3].replace(" ", "").replace(",", ".")) for o in ops if not o[3].startswith("+"))
    fmt = lambda v: f"{v:,.2f}".replace(",", " ").replace(".", ",")
    tin, tout = totals or (fmt(inc), fmt(out))
    p = []
    p += w(45, 68, "Выписка по платёжному счёту")
    p += w(45, 94, "За период 01.01.2026 — 31.01.2026")
    p += w(57, 130, "Владелец счёта")
    p += w(57, 142, holder)
    p += w(349, 147, "Остаток на 01.01.2026") + w(530, 147, opening)
    p += w(349, 163, "Пополнение") + w(510, 163, tin)
    p += w(57, 167, "Номер счёта") + w(195, 167, SBER_ACCOUNT)
    p += w(349, 177, "Списание") + w(510, 177, tout)
    p += w(349, 191, "Остаток на 31.01.2026") + w(530, 191, closing)
    p += w(57, 204, "Валюта") + w(195, 204, "Российский рубль")
    p += (w(45, 299, "ДАТА ОПЕРАЦИИ") + w(109, 299, "(МСК)") + w(152, 299, "КАТЕГОРИЯ")
          + w(370, 299, "СУММА В ВАЛЮТЕ СЧЁТА") + w(490, 299, "ОСТАТОК СРЕДСТВ"))
    p += w(45, 314, "Дата обработки1") + w(152, 314, "Описание операции")
    p += w(45, 324, "и код авторизации")
    top = 350.0
    for date, time, cat, amount, balance, desc in ops:
        p += w(45, top, date) + w(102, top, time) + w(152, top, cat) + w(420, top, amount) + w(525, top, balance)
        p += w(45, top + 11, date) + w(102, top + 11, "123456") + w(152, top + 11, desc)
        top += 28
    p += w(45, top + 40, "Дата формирования документа 31.01.2026")
    return [p]


SBER_OPS = [  # от новых к старым, как в настоящей выписке
    ("20.01.2026", "12:00", "Перевод СБП", "50 000,00", "10,00", "Т-Банк. Операция по счету ****0002"),
    ("15.01.2026", "15:00", "Супермаркеты", "990,00", "50 010,00", "SHOP. Операция по карте ****1111"),
    ("15.01.2026", "07:00", "Прочие операции", "+50 000,00", "51 000,00", "Заработная плата. Операция по счету ****0002"),
]


class SberParseTest(unittest.TestCase):
    def test_parse_and_balances(self):
        s = parse_sber(sber_pages(SBER_OPS, "1 000,00", "10,00"), "c" * 64)
        self.assertEqual(s.account, "40817810000000000002")
        self.assertEqual(s.holder, HOLDER)
        self.assertEqual((s.opening_minor, s.closing_minor), (100000, 1000))
        self.assertEqual([o.amount_minor for o in s.operations], [-5000000, -99000, 5000000])
        self.assertEqual([o.kind.kind for o in s.operations], ["transfer_out_sbp", "card_payment", "salary"])
        self.assertEqual(s.operations[1].card_last4, "1111")
        self.assertEqual(s.check_totals(), [])

    def test_missing_operation_detected_by_balances(self):
        broken = [SBER_OPS[0], SBER_OPS[2]]  # потеряна покупка
        s = parse_sber(sber_pages(broken, "1 000,00", "10,00", totals=("50 000,00", "50 990,00")), "c" * 64)
        problems = s.check_totals()
        self.assertTrue(any("Остатки" in p for p in problems))
        self.assertTrue(any("Списания" in p or "Расходы" in p for p in problems))

    def test_wrong_document(self):
        with self.assertRaises(StatementParseError):
            parse_sber([w(45, 68, "Что-то другое")])


TBANK_OPS = [  # синтетическая справка Т-Банка того же владельца
    dict(date="20.01.2026", time="12:00", amount="+50 000.00 ₽", desc_lines=["Пополнение. Система", "быстрых платежей"]),
    dict(date="18.01.2026", time="10:00", amount="-2 000.00 ₽", desc_lines=["Оплата в CAFE"]),
]


def load(conn, sber_ops=SBER_OPS, sber_holder=HOLDER, tbank_ops=TBANK_OPS, t_in="50 000,00 ₽", t_out="2 000,00 ₽",
         opening="1 000,00", closing="10,00"):
    t = parse_tbank(tbank_ops, total_in=t_in, total_out=t_out)
    t.holder = HOLDER
    import_statement(conn, t, "t.pdf")
    s = parse_sber(sber_pages(sber_ops, opening, closing, holder=sber_holder), "d" * 64)
    import_statement(conn, s, "s.pdf")


class TransferMatchTest(unittest.TestCase):
    def setUp(self):
        self.conn = connect(":memory:")
        self.addCleanup(self.conn.close)

    def test_pair_found_and_counted_as_own(self):
        load(self.conn)
        new, ambiguous = match_transfers(self.conn)
        self.assertEqual((new, ambiguous), (1, []))
        self.assertEqual(len(list_links(self.conn)), 1)
        text = build_summary(self.conn)
        # Зарплата — внешний доход; перевод Сбер → Т-Банк не задваивает её.
        self.assertIn("| Внешние: доходы и расходы | 50 000,00 ₽ | 2 990,00 ₽ | 3 |", text)
        self.assertIn("| Между своими счетами | 50 000,00 ₽ | 50 000,00 ₽ | 2 |", text)
        self.assertIn("| Не уточнено | 0,00 ₽ | 0,00 ₽ | 0 |", text)

    def test_rerun_does_not_duplicate(self):
        load(self.conn)
        match_transfers(self.conn)
        self.assertEqual(match_transfers(self.conn), (0, []))

    def test_other_holder_not_matched(self):
        load(self.conn, sber_holder="Петров Пётр Петрович")
        self.assertEqual(match_transfers(self.conn)[0], 0)

    def test_time_window(self):
        late = [("20.01.2026", "12:30", *SBER_OPS[0][2:])] + SBER_OPS[1:]
        load(self.conn, sber_ops=late)
        self.assertEqual(match_transfers(self.conn)[0], 0)

    def test_ambiguous_left_for_user(self):
        two = [TBANK_OPS[0], dict(TBANK_OPS[0], time="12:03"), TBANK_OPS[1]]
        load(self.conn, tbank_ops=two, t_in="100 000,00 ₽")
        new, ambiguous = match_transfers(self.conn)
        self.assertEqual(new, 0)
        self.assertEqual(len(ambiguous), 1)

    def test_bad_balances_rejected(self):
        s = parse_sber(sber_pages(SBER_OPS, "1 000,00", "11,00"), "e" * 64)
        with self.assertRaises(ImportRejected):
            import_statement(self.conn, s, "bad.pdf")


class MigrationTest(unittest.TestCase):
    def test_old_registry_gets_new_columns(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "old.sqlite")
            old = sqlite3.connect(path)
            old.executescript("""
                CREATE TABLE imports (id INTEGER PRIMARY KEY, file_sha256 TEXT NOT NULL UNIQUE, file_name TEXT NOT NULL,
                  imported_at TEXT NOT NULL, source TEXT NOT NULL, issue_number TEXT, account TEXT NOT NULL,
                  period_from TEXT, period_to TEXT, total_in_minor INTEGER, total_out_minor INTEGER,
                  ops_in_file INTEGER NOT NULL, ops_new INTEGER NOT NULL, ops_known INTEGER NOT NULL);
            """)
            old.close()
            conn = connect(path)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(imports)")}
            self.assertTrue({"bank", "holder", "opening_minor", "closing_minor"} <= cols)
            conn.close()


if __name__ == "__main__":
    unittest.main()
