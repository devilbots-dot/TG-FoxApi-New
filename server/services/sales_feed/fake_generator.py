"""
Fake sales data generator.

The generated event is display-only. It never creates an order, changes stock,
changes a balance, or writes a business record.
"""

from __future__ import annotations

import random
from typing import Literal


_DEFAULT_COUNTRIES: list[tuple[str, str, float]] = [
    ("TH", "Thailand", 0.50),
    ("IN", "India", 0.29),
]

_PAYMENT_METHODS = [
    "OxaPay (USDT TRC20)",
    "OxaPay (USDT BEP20)",
    "Crypto Wallet",
    "USDT TRC20",
    "USDT BEP20",
    "In-App Balance",
]

_DEVICES = [
    "iPhone 14 Pro", "Samsung Galaxy S23", "Xiaomi 13", "OnePlus 11",
    "Google Pixel 7", "iPhone 13", "Redmi Note 12", "iPhone 15",
    "Samsung Galaxy A54", "OPPO Reno 10", "Vivo V27", "Realme GT Neo 5",
    "Motorola Edge 40", "iPhone SE", "Huawei P60",
]

_FIRST_NAMES = [
    "alex", "max", "ivan", "kate", "anna", "john", "mike", "sara", "leo",
    "ali", "raj", "sam", "tom", "dan", "nina", "zara", "omar", "lena",
    "kim", "jay", "ben", "amy", "mia", "ash", "bob", "lucy", "erik",
    "mark", "luke", "noah", "liam", "emma", "jane", "dave", "rose",
]

_SUFFIXES = [
    "2024", "pro", "official", "real", "dev", "x", "01", "99", "007",
    "_tg", "_ok", "_here", "555", "100", "777", "xo", "user",
]


def _random_username() -> str:
    base = random.choice(_FIRST_NAMES)
    style = random.randint(0, 3)
    if style == 0:
        return base + str(random.randint(10, 9999))
    if style == 1:
        return base + random.choice(_SUFFIXES)
    if style == 2:
        return base + random.choice(_FIRST_NAMES)
    return base + "_" + str(random.randint(1, 999))


def _random_country(country_pool: list | None) -> tuple[str, str, float]:
    if country_pool:
        entry = random.choice(country_pool)
        return (
            str(entry.get("code", "US")).upper(),
            str(entry.get("name", "United States")),
            float(entry.get("price", 2.00)),
        )
    return random.choice(_DEFAULT_COUNTRIES)


def _apply_randomization(base_price: float, level: int) -> float:
    max_variation = (max(1, min(10, int(level))) - 1) * 0.015
    if max_variation <= 0:
        return round(base_price, 2)
    raw = base_price * (1 + random.uniform(-max_variation, max_variation))
    return max(0.10, round(round(raw * 10) / 10, 2))


def generate_fake_event(
    *,
    product_pool: list[str] | None = None,
    country_pool: list | None = None,
    randomization_level: int = 5,
) -> dict:
    pool = product_pool or ["account", "session"]
    product_type: Literal["account", "session"] = random.choice(pool)
    code, name, base_price = _random_country(country_pool)
    price_per = _apply_randomization(base_price, randomization_level)
    quantity = random.randint(1, 4) if product_type == "session" else 1
    return {
        "is_fake": True,
        "product_type": product_type,
        "country_code": code,
        "country_name": name,
        "quantity": quantity,
        "price_per": price_per,
        "total_price": round(price_per * quantity, 2),
        "fake_username": _random_username(),
        "payment_method": random.choice(_PAYMENT_METHODS),
        "device": random.choice(_DEVICES),
    }