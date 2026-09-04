"""Parser tests for the Amazon "Order History" export.

The fixture is scrubbed (names/addresses replaced) but structurally real:
multi-shipment orders, a gift-card split payment, per-row totals that must be
summed per shipment — the exact traps the export sets for a naive reader.
"""
from datetime import date
from decimal import Decimal

import pytest

from app.services.amazon_import_service import detect_format, parse_order_history

from pathlib import Path

FIXTURE = Path(__file__).parent / "fixtures" / "amazon_order_history_sample.csv"


def _fixture():
    return FIXTURE.read_bytes()


def test_detect_format_accepts_fixture_and_rejects_plain_statements():
    assert detect_format(_fixture())
    assert not detect_format(b"date,description,amount\n2026-01-01,COFFEE,4.50\n")


def test_fixture_parses_to_charge_per_shipment_not_per_order():
    charges = parse_order_history(_fixture())
    assert len(charges) == 15

    by_order = {}
    for c in charges:
        by_order.setdefault(c.order_id, []).append(c)
    # Two orders shipped as four separately-charged shipments each.
    assert len(by_order["112-5796650-2981858"]) == 4
    assert len(by_order["111-4273393-8969034"]) == 4


def test_shipment_amount_sums_item_rows():
    charges = parse_order_history(_fixture())
    by_tracking = {c.tracking: c for c in charges}
    two_item_shipment = by_tracking["AMZN_US(TBA306151649688)"]
    # 12.11 + 51.70 — the two item rows of one shipment, charged together.
    assert two_item_shipment.amount == Decimal("63.81")
    assert len(two_item_shipment.items) == 2


def test_card_last4_and_split_payment_are_parsed():
    charges = parse_order_history(_fixture())
    assert all(c.card_last4 for c in charges)

    split = [c for c in charges if c.is_split_payment]
    assert len(split) == 1
    assert split[0].card_last4 == "7944"
    # Gift card covered part of this shipment: the card saw less than 27.06,
    # so exact-amount matching must never fire on it (matcher's job).
    assert split[0].amount == Decimal("27.06")


def test_non_card_and_zero_rows_are_dropped():
    # Compact export-shaped file: only the columns the parser reads, but in
    # the export's quoting/comma style.
    header = (
        "Order ID,Total Amount,Product Name,Ship Date,Payment Method Type,"
        "Currency,Carrier Name & Tracking Number,Order Date"
    )
    rows = [
        # Gift-card-only payment: never touches a card statement.
        "111-1,10.00,Thing,2026-01-02T00:00:00Z,Gift Certificate,USD,TBA123,2026-01-01T00:00:00Z",
        # Zero total (refund-adjusted row).
        "111-2,0.00,Thing,2026-01-04T00:00:00Z,Visa - 9371,USD,TBA456,2026-01-03T00:00:00Z",
        # A normal charge: the only survivor.
        "111-3,27.50,Thing,2026-01-06T00:00:00Z,Visa - 9371,USD,TBA789,2026-01-05T00:00:00Z",
    ]
    charges = parse_order_history(("\r\n".join([header, *rows]) + "\r\n").encode())
    assert len(charges) == 1
    assert charges[0].amount == Decimal("27.50")
    assert charges[0].card_last4 == "9371"


def test_missing_columns_raise_a_clear_error():
    with pytest.raises(ValueError, match="Not an Amazon Order History export"):
        parse_order_history(b"a,b,c\n1,2,3\n")


def test_real_world_ship_date_formats():
    # Real exports mix full timestamps, millisecond timestamps and
    # "Not Available"; the last two must anchor on the order date.
    header = (
        "Order ID,Total Amount,Product Name,Ship Date,Payment Method Type,"
        "Currency,Carrier Name & Tracking Number,Order Date"
    )
    rows = [
        "111-1,10.00,Thing,2026-05-05T10:54:00.704Z,Visa - 9371,USD,TBA1,2026-05-04T00:00:00Z",
        "111-2,10.00,Thing,Not Available,Visa - 9371,USD,TBA2,2026-05-06T00:00:00Z",
    ]
    charges = parse_order_history(("\r\n".join([header, *rows]) + "\r\n").encode())
    assert [c.ship_date for c in charges] == [date(2026, 5, 5), date(2026, 5, 6)]


def test_compound_tracking_never_auto_matches():
    # "Shipped and Shipped" rows name two tracking numbers: the row total may
    # span two separately-charged shipments, so it must be report-only.
    header = (
        "Order ID,Total Amount,Product Name,Ship Date,Payment Method Type,"
        "Currency,Carrier Name & Tracking Number,Order Date"
    )
    row = (
        "111-1,40.00,Thing,2026-05-05T10:54:00Z,Visa - 9371,USD,"
        "AMZN_US(TBA123) and AMZN_US(TBA456),2026-05-04T00:00:00Z"
    )
    charges = parse_order_history(("\r\n".join([header, row]) + "\r\n").encode())
    assert len(charges) == 1
    assert charges[0].is_split_payment
