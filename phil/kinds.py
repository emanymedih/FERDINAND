"""Тип операции по форме записи в справке Т-Банка.

Это не экономический смысл: «Внутренний перевод на договор» может быть
переводом между своими продуктами, возвратом долга или подарком. Тип
определяет только код по тексту источника; смысл уточняется отдельно
(пользователь или ИИ) и хранится в дополнительном слое.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Порядок важен: первое совпадение побеждает.
RULES: list[tuple[str, re.Pattern]] = [
    ("card_payment", re.compile(r"^Оплата в (?P<cp>.+)$")),
    ("service_payment", re.compile(r"^Оплата услуг (?P<cp>.+)$")),
    ("transfer_out_internal", re.compile(r"^Внутренний перевод на договор (?P<ref>\d+)")),
    ("savings_out", re.compile(r"^Внутренний перевод на вклад (?P<ref>\d+)")),
    ("transfer_out_card", re.compile(r"^Внутренний перевод на карту (?P<ref>.+)$")),
    ("transfer_in_internal", re.compile(r"^(?:Внутрибанковский перевод|Перевод) с договора\s*(?P<ref>\d+)?")),
    ("transfer_out_phone", re.compile(r"^Внешний перевод по номеру телефона (?P<ref>\+?\d+)")),
    ("transfer_out_bank", re.compile(r"^Внешний банковский перевод сч[её]т (?P<ref>\d{20}),?\s*(?P<cp>.*)$")),
    ("transfer_out_card", re.compile(r"^Перевод на карту\s*(?P<ref>.*)$")),
    ("transfer_self", re.compile(r"^Перевод себе")),
    ("topup", re.compile(r"^Пополнение\. (?P<cp>.+)$")),
    ("bank_transfer_in", re.compile(r"^Банковский перевод\. (?P<cp>.+)$")),
    ("savings_out", re.compile(r"^Пополнение Инвесткопилки")),
    ("savings_in", re.compile(r"^Закрытие вклада")),
    ("cash_in", re.compile(r"^Внесение наличных")),
    ("cash_out", re.compile(r"^Снятие наличных\.?\s*(?P<cp>.*)$")),
    ("bank_fee", re.compile(r"^Плата за (?P<cp>.+)$")),
    ("cashback", re.compile(r"^Кэшбэк")),
    ("refund", re.compile(r"^Возврат покупки(?: через СБП)?\s*(?P<cp>.*)$")),
]

KIND_TITLES = {
    "card_payment": "Покупки",
    "service_payment": "Оплата услуг",
    "transfer_out_internal": "Переводы на договоры Т-Банка",
    "transfer_in_internal": "Переводы с договоров Т-Банка",
    "transfer_out_phone": "Переводы по номеру телефона",
    "transfer_out_bank": "Переводы на счета в других банках",
    "transfer_out_card": "Переводы на карту",
    "transfer_self": "Переводы себе",
    "topup": "Пополнения извне",
    "bank_transfer_in": "Входящие банковские переводы",
    "savings_out": "В копилку и вклады",
    "savings_in": "Из копилки и вкладов",
    "cash_in": "Внесение наличных",
    "cash_out": "Снятие наличных",
    "bank_fee": "Платы банку",
    "cashback": "Кэшбэк",
    "refund": "Возвраты покупок",
    "unknown": "Не распознано",
}

# Типы, у которых без уточнения неизвестно, расход это или перемещение денег.
NEEDS_MEANING = {
    "transfer_out_internal", "transfer_in_internal", "transfer_out_phone",
    "transfer_out_bank", "transfer_out_card", "topup", "cash_in", "cash_out",
}


@dataclass(frozen=True)
class Kind:
    kind: str
    counterparty: str | None
    counterparty_ref: str | None


def classify(description: str) -> Kind:
    for kind, rx in RULES:
        m = rx.match(description)
        if m:
            g = m.groupdict()
            cp = (g.get("cp") or "").strip() or None
            ref = (g.get("ref") or "").strip() or None
            return Kind(kind, cp, ref)
    return Kind("unknown", None, None)
