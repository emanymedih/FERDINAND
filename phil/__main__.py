"""Командная строка Фила.

    python -m phil import data/выписка.pdf [data/сбер.pdf ...]   # Т-Банк, Сбер или Озон Банк, определяется сам
    python -m phil transfers                                     # найденные переводы между своими счетами
    python -m phil summary [--from 2026-01-01] [--to 2026-01-31] [--out data/сводка.md]
    python -m phil counterparties [--all] [--top 30]
    python -m phil label 4373 own [--note "накопительный счёт"]
    python -m phil label-kind cash_in own [--note "свои отложенные деньги"]

Реестр по умолчанию лежит в data/phil.sqlite; папка data/ исключена из Git.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .labels import LABELS, counterparties, set_kind_label, set_label
from .registry import ImportRejected, connect, import_statement
from .summary import build_summary
from .kinds import KIND_TITLES
from . import ozon_pdf, sber_pdf, tbank_pdf
from .tbank_pdf import StatementParseError, fmt_minor
from .transfers import list_links, match_transfers

DEFAULT_DB = Path("data/phil.sqlite")


def parse_any(path: Path):
    """Определяет банк по первой странице и вызывает нужный адаптер."""
    import pdfplumber

    with pdfplumber.open(path) as pdf:
        head = pdf.pages[0].extract_text() or "" if pdf.pages else ""
    if "Выписка по платёжному счёту" in head or "Выписка по платежному счету" in head:
        return sber_pdf.parse_pdf(path)
    if "ОЗОН Банк" in head and "Справка о движении средств" in head:
        return ozon_pdf.parse_pdf(path)
    if "Справка о движении средств" in head:
        return tbank_pdf.parse_pdf(path)
    raise StatementParseError("Формат не распознан: поддерживаются Т-Банк, Сбер и Озон Банк")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="phil", description="Фил: личные финансы")
    p.add_argument("--db", type=Path, default=DEFAULT_DB, help="файл реестра")
    sub = p.add_subparsers(dest="cmd", required=True)

    imp = sub.add_parser("import", help="загрузить выписку Т-Банка, Сбера или Озон Банка (PDF)")
    imp.add_argument("files", nargs="+", type=Path)

    summ = sub.add_parser("summary", help="сводка за период")
    summ.add_argument("--from", dest="date_from")
    summ.add_argument("--to", dest="date_to")
    summ.add_argument("--top", type=int, default=10)
    summ.add_argument("--out", type=Path)

    cps = sub.add_parser("counterparties", help="получатели и отправители переводов по сумме")
    cps.add_argument("--all", action="store_true", help="показать и уже отмеченные")
    cps.add_argument("--top", type=int, default=30)

    lab = sub.add_parser("label", help="отметить реквизит: own — свой счёт, external — чужой")
    lab.add_argument("ref", help="договор, телефон или счёт; можно последние цифры")
    lab.add_argument("label", choices=sorted(LABELS))
    lab.add_argument("--note")

    labk = sub.add_parser("label-kind", help="отметить все операции одного типа, например cash_in")
    labk.add_argument("kind")
    labk.add_argument("label", choices=sorted(LABELS))
    labk.add_argument("--note")

    sub.add_parser("transfers", help="переводы между своими счетами, найденные по сумме и времени")

    args = p.parse_args(argv)
    conn = connect(args.db)

    if args.cmd == "import":
        failed = 0
        for f in args.files:
            try:
                stmt = parse_any(f)
                res = import_statement(conn, stmt, f.name)
            except (StatementParseError, ImportRejected) as e:
                print(f"{f.name}: не загружено — {e}", file=sys.stderr)
                failed += 1
                continue
            checks = [f"поступления {fmt_minor(stmt.total_in_minor)}", f"списания {fmt_minor(stmt.total_out_minor)}"]
            if stmt.opening_minor is not None:
                checks.append(f"остатки {fmt_minor(stmt.opening_minor)} → {fmt_minor(stmt.closing_minor)}")
            print(f"{f.name} ({stmt.bank} …{stmt.account[-4:]}): период {stmt.period_from} — {stmt.period_to}, "
                  f"операций {res.ops_in_file}, новых {res.ops_new}, уже были {res.ops_known}. "
                  f"Сошлось: {', '.join(checks)}.")
            for w in stmt.warnings:
                print(f"  предупреждение: {w}", file=sys.stderr)
        new, ambiguous = match_transfers(conn)
        if new:
            print(f"Найдено переводов между своими счетами: {new} (python3 -m phil transfers)")
        for a in ambiguous:
            print(f"  неоднозначно, не связано: {a}", file=sys.stderr)
        return 1 if failed else 0

    if args.cmd == "transfers":
        links = list_links(conn)
        print("| Списано | Откуда | Поступило | Куда | Сумма | Разница, мин |")
        print("| --- | --- | --- | --- | ---: | ---: |")
        for l in links:
            print(f"| {l['out_at']} | …{l['out_account'][-4:]} {l['out_desc']} | {l['in_at']} | "
                  f"…{l['in_account'][-4:]} {l['in_desc']} | {fmt_minor(l['amount'])} | {l['delta_minutes']} |")
        print(f"\nПар: {len(links)}.")
        return 0

    if args.cmd == "counterparties":
        items = [c for c in counterparties(conn) if args.all or not c["label"]]
        print("| Реквизит | Тип | Ушло | Пришло | Операций | Период | Отметка |")
        print("| --- | --- | ---: | ---: | ---: | --- | --- |")
        for c in items[: args.top]:
            hint = "в обе стороны" if c["both_ways"] else ""
            print(f"| {c['ref']} | {', '.join(KIND_TITLES[k] for k in sorted(c['kinds']))} | "
                  f"{fmt_minor(c['out_minor'])} | {fmt_minor(c['in_minor'])} | {c['n']} | "
                  f"{c['first']} — {c['last']} | {c['label'] or hint} |")
        print(f"\nПоказано {min(len(items), args.top)} из {len(items)}.")
        return 0

    if args.cmd == "label":
        try:
            ref = set_label(conn, args.ref, args.label, args.note)
        except ValueError as e:
            print(e, file=sys.stderr)
            return 1
        print(f"{ref}: {LABELS[args.label]}")
        return 0

    if args.cmd == "label-kind":
        try:
            set_kind_label(conn, args.kind, args.label, args.note)
        except ValueError as e:
            print(e, file=sys.stderr)
            return 1
        print(f"{KIND_TITLES[args.kind]}: {LABELS[args.label]}")
        return 0

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
