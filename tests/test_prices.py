from decimal import Decimal as D

import pytest

from bfp.parse.prices import is_purchasable, normalize_availability, parse_pct, parse_price


@pytest.mark.parametrize(
    "text,expected",
    [
        ("1.299,00 €", D("1299.00")),
        ("€ 1.299", D("1299.00")),
        ("1299.00", D("1299.00")),
        ("12,99", D("12.99")),
        ("12,9", D("12.90")),
        ("EUR 49.90", D("49.90")),
        ("da 49,90 € a 59,90 €", D("49.90")),
        ("1 299,00 €", D("1299.00")),
        ("1 299,00 €", D("1299.00")),
        ("199,-", D("199.00")),
        ("1,299.50", D("1299.50")),
        ("0,99€", D("0.99")),
        ("gratis", None),
        ("0,00 €", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_price_display(text, expected):
    assert parse_price(text) == expected


@pytest.mark.parametrize(
    "value,expected",
    [("649.90", D("649.90")), (649.9, D("649.90")), (1299, D("1299.00")), ("12.990", D("12.99")), ("1.299,00", D("1299.00"))],
)
def test_parse_price_machine(value, expected):
    assert parse_price(value, machine=True) == expected


@pytest.mark.parametrize(
    "text,expected",
    [("-20%", D("20")), ("Sconto 20 %", D("20")), ("−12,5%", D("12.5")), ("fino al -50%", D("50")), ("100%", None), ("20", None)],
)
def test_parse_pct(text, expected):
    assert parse_pct(text) == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        ("https://schema.org/InStock", "InStock"),
        ("http://schema.org/OutOfStock", "OutOfStock"),
        ("InStock", "InStock"),
        ("in_stock", "InStock"),
        ("PreOrder", "PreOrder"),
        ("Non disponibile", "OutOfStock"),
        ("Esaurito", "OutOfStock"),
        ("Disponibile, spedizione in 24h", "InStock"),
        (["https://schema.org/LimitedAvailability"], "LimitedAvailability"),
        ("boh", None),
    ],
)
def test_availability(value, expected):
    assert normalize_availability(value) == expected


def test_purchasable():
    assert is_purchasable("InStock") and is_purchasable(None) and is_purchasable("PreOrder")
    assert not is_purchasable("OutOfStock") and not is_purchasable("SoldOut")
