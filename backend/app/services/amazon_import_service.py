"""Parse Amazon's "Order History" privacy export into charge candidates.

The export is one row per shipment item, not one row per order or per charge:
a single order split across shipments appears as several rows with distinct
tracking numbers, each of which was a separate credit-card charge. So the unit
this parser produces is the *charge* — one shipment — summed from its item rows
by ``Total Amount`` (which already folds in item tax and shipping).

Rows that pay without a card (gift-card-only, Amazon Rewards, blank payment
method) cannot appear on a card statement, so they are dropped here rather than
surfaced as unmatched noise. Split payments (gift card + card) are kept but
flagged: the card was charged less than ``Total Amount``, so they can only ever
be soft-suggested against a statement, never exact-amount matched.

Pure functions — no DB access — mirroring ``import_service.parse_*``.
"""
import csv
import io
import re
from datetime import date as _date
from decimal import Decimal

from app.schemas.amazon import AmazonCharge

# A charge must carry at least these to be matchable. "Total Amount" is what we
# sum, "Payment Method Type" tells us which card (and whether it was a split),
# and Ship Date anchors the statement-window search.
REQUIRED_COLUMNS = (
    "order id",
    "total amount",
    "product name",
    "ship date",
    "payment method type",
)

# "Visa - 9371", "Gift Certificate/Card and Visa - 7944" — the trailing four
# digits are the only stable link back to a card account.
_CARD_LAST4_RE = re.compile(r"(\d{4})\s*$")

_MAX_ITEMS_PER_CHARGE = 12


def detect_format(content: bytes) -> bool:
    """True if the CSV header carries Amazon's Order History columns."""
    first_line = content.split(b"\n", 1)[0]
    try:
        text = first_line.decode("utf-8-sig")
    except UnicodeDecodeError:
        return False
    cols = {c.strip().lower() for c in text.split(",")}
    return all(col in cols for col in REQUIRED_COLUMNS)


def _parse_date(value: str):
    """Accept '2022-05-26T02:17:15Z' (Order/Ship Date) or a plain date."""
    value = (value or "").strip()
    if len(value) >= 10:
        try:
            return _date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _parse_payment(pm: str) -> tuple[str | None, bool]:
    """Return (card_last4, is_split_payment) from a Payment Method Type cell.

    A trailing four-digit group names the card that paid; "Gift Certificate" or
    "Rewards" alongside it means the card covered only part of the charge.
    """
    match = _CARD_LAST4_RE.search(pm or "")
    if not match:
        return None, False
    is_split = ("Gift Certificate" in pm) or ("Rewards" in pm)
    return match.group(1), is_split


def parse_order_history(content: bytes) -> list[AmazonCharge]:
    """Parse an Amazon Order History CSV into per-shipment charge candidates."""
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("latin-1", errors="replace")

    reader = csv.DictReader(io.StringIO(text))
    fieldnames = [f.lower().strip() for f in (reader.fieldnames or [])]
    missing = [c for c in REQUIRED_COLUMNS if c not in fieldnames]
    if missing:
        raise ValueError(
            "Not an Amazon Order History export (missing columns: "
            + ", ".join(missing) + ")"
        )

    # Charge identity = (Order ID, tracking, ship date). Tracking is the
    # shipment key — and 89 of them are reused across *different* orders in a
    # real export, so order_id must stay in the key. Without tracking, rows
    # group per ship date instead (documented risk: two same-day untracked
    # shipments of one order would merge, which errs toward under-matching,
    # never a wrong link). Dates arrive as full ISO timestamps or as
    # "Not Available"; both anchor on the ship date when parseable, else the
    # order date.
    charges: dict[tuple, AmazonCharge] = {}

    for row in reader:
        row = {(k or "").lower().strip(): (v or "") for k, v in row.items()}

        order_id = row.get("order id", "").strip()
        if not order_id:
            continue

        try:
            total = Decimal(row.get("total amount", "").strip())
        except Exception:
            continue
        if total <= 0:
            continue

        ship_date = _parse_date(row.get("ship date", "")) or _parse_date(row.get("order date", ""))
        if ship_date is None:
            continue

        last4, is_split = _parse_payment(row.get("payment method type", ""))
        if last4 is None:
            # Paid without a card (gift card / rewards / blank) — it will never
            # appear on a card statement, so there is nothing to match against.
            continue

        tracking = row.get("carrier name & tracking number", "").strip()
        if tracking in ("Not Available", "Not Applicable"):
            tracking = ""
        # "AMZN_US(TBA…63204) and AMZN_US(TBA…832804)" — one item split across
        # two shipments (status "Shipped and Shipped"), which Amazon may have
        # charged as one combined charge or two. Which one it was is unknowable
        # from the export, so flag it like a split payment: report-only.
        compound = " and " in tracking

        key = (order_id, tracking, ship_date)
        charge = charges.get(key)
        if charge is None:
            charge = AmazonCharge(
                order_id=order_id,
                tracking=tracking,
                ship_date=ship_date,
                order_date=_parse_date(row.get("order date", "")),
                amount=Decimal("0"),
                currency=(row.get("currency", "").strip().upper() or "USD"),
                card_last4=last4,
                is_split_payment=is_split or compound,
                items=[],
            )
            charges[key] = charge
        else:
            # A shipment paid by two cards is rare; if it happens, refuse to
            # exact-match it rather than guess which card carried the balance.
            if charge.card_last4 != last4:
                charge.is_split_payment = True
            if is_split:
                charge.is_split_payment = True

        charge.amount += total
        name = row.get("product name", "").strip()
        if name and name not in charge.items and len(charge.items) < _MAX_ITEMS_PER_CHARGE:
            charge.items.append(name)

    for charge in charges.values():
        charge.amount = charge.amount.quantize(Decimal("0.01"))

    return list(charges.values())
