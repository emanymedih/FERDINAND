"""Командная строка Фила.

    python -m phil import data/выписка.pdf
    python -m phil summary [--from 2026-01-01] [--to 2026-01-31] [--out data/сводка.md]

Реестр по умолчанию лежит в data/phil.sqlite; папка data/ исключена из Git.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .registry import ImportRejected, connect, import_statement
from .summary import build_summary
from .tbank_pdf import StatementParseError, fmt_minor, parse_pdf

DEFAULT_DB = Path("data/phil.sqlite")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="phil", description="Фил: личные финансы")
    p.add_argument("--db", type=Path, default=DEFAULT_DB, help="файл реестра")
    sub = p.add_subparsers(dest="cmd", required=True)

    imp = sub.add_parser("import", help="загрузить справку о движении средств Т-Банка (PDF)")
    imp.add_argument("files", nargs="+", type=Path)

    summ = sub.add_parser("summary", help="сводка за период")
    summ.add_argument("--from", dest="date_from")
    summ.add_argument("--to", dest="date_to")
    summ.add_argument("--top", type=int, default=10)
    summ.add_argument("--out", type=Path)

    args = p.parse_args(argv)
    conn = connect(args.db)

    if args.cmd == "import":
        failed = 0
        for f in args.files:
            try:
                stmt = parse_pdf(f)
                res = import_statement(conn, stmt, f.name)
            except (StatementParseError, ImportRejected) as e:
                print(f"{f.name}: не загружено — {e}", file=sys.stderr)
                failed += 1
                continue
            print(f"{f.name}: период {stmt.period_from} — {stmt.period_to}, операций {res.ops_in_file}, "
                  f"новых {res.ops_new}, уже были {res.ops_known}. Итоги справки сошлись: "
                  f"пополнения {fmt_minor(stmt.total_in_minor)}, расходы {fmt_minor(stmt.total_out_minor)}.")
            for w in stmt.warnings:
                print(f"  предупреждение: {w}", file=sys.stderr)
        return 1 if failed else 0

    if args.cmd == "summary":
        text = build_summary(conn, args.date_from, args.date_to, args.top)
        if args.out:
            args.out.write_text(text, encoding="utf-8")
            print(f"Сводка сохранена: {args.out}")
        else:
            print(text)
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
