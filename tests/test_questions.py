"""Вопросы Фила и архив реестра. Только синтетические данные."""

import os
import tempfile
import unittest
import zipfile
from pathlib import Path

from phil.__main__ import backup, restore
from phil.labels import set_label
from phil.questions import build_questions, format_questions
from phil.registry import connect, import_statement
from tests.test_phil import parse

OPS = [  # регулярный платёж 34 000 ₽ в двух месяцах и разовый перевод
    dict(date="10.02.2026", time="12:00", amount="-34 000.00 ₽", desc_lines=["Внутренний перевод на", "договор 5000000009"]),
    dict(date="05.02.2026", time="12:00", amount="-60 000.00 ₽", desc_lines=["Внешний перевод по", "номеру телефона", "+79000000002"]),
    dict(date="10.01.2026", time="12:00", amount="-34 000.00 ₽", desc_lines=["Внутренний перевод на", "договор 5000000009"]),
    dict(date="09.01.2026", time="12:00", amount="-500.00 ₽", desc_lines=["Внутренний перевод на", "договор 5000000008"]),
]


class QuestionsTest(unittest.TestCase):
    def setUp(self):
        self.conn = connect(":memory:")
        self.addCleanup(self.conn.close)
        import_statement(self.conn, parse(OPS, total_in="0,00 ₽", total_out="128 500,00 ₽"), "x.pdf")

    def test_regular_first_small_skipped(self):
        qs = build_questions(self.conn)
        self.assertEqual([q["subject"] for q in qs], ["5000000009", "+79000000002"])
        self.assertTrue(qs[0]["regular"])
        self.assertFalse(qs[1]["regular"])
        self.assertIn("Регулярно", format_questions(qs))

    def test_answered_disappears(self):
        set_label(self.conn, "5000000009", "external", "Аренда")
        self.assertEqual([q["subject"] for q in build_questions(self.conn)], ["+79000000002"])

    def test_period_filter(self):
        self.assertEqual([q["subject"] for q in build_questions(self.conn, "2026-02-01")],
                         ["+79000000002", "5000000009"])


class BackupTest(unittest.TestCase):
    def test_roundtrip_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d, "a", "phil.sqlite")
            conn = connect(src)
            conn.close()
            Path(d, "a", "x.pdf").write_bytes(b"%PDF-test")
            archive = backup(src)
            dst = Path(d, "b", "phil.sqlite")
            self.assertEqual(restore(archive, dst), 0)
            self.assertTrue(dst.exists() and Path(d, "b", "x.pdf").exists())
            self.assertEqual(restore(archive, dst), 1)  # существующий реестр не трогаем

    def test_rejects_paths_in_archive(self):
        with tempfile.TemporaryDirectory() as d:
            archive = Path(d, "evil.zip")
            with zipfile.ZipFile(archive, "w") as z:
                z.writestr("../evil.pdf", b"x")
            self.assertEqual(restore(archive, Path(d, "c", "phil.sqlite")), 0)
            self.assertFalse(os.path.exists(Path(d, "evil.pdf")))


if __name__ == "__main__":
    unittest.main()
