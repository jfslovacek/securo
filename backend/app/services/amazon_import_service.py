"""Parse Amazon's "Order History" privacy export into charge candidates.

The export is one row per shipment item, not one row per order or per charge:
a single order split across shipments appears as several rows with distinct
tracking numbers, each of which was a separate card charge. So the unit this
parser produces is the *charge* — one shipment — and its matchable amount is
the shipment's ``Shipment Item Subtotal``: the pre-tax subtotal of exactly
that shipment, repeated on every item row belonging to it. (Verified on a
real export: all 296 multi-item shipment groups repeat one subtotal value.)
``Total Amount`` is per item and includes tax, so summing it overshoots the
statement charge by the tax — which is why the subtotal, not the sum, is
what actually appears on a card statement. When an export carries no
subtotal column or value, we fall back to summing ``Total Amount``.

Rows that pay without a card (gift-card-only, Amazon Rewards, blank payment
method) cannot appear on a card statement, so they are dropped here rather
than surfaced as unmatched noise. Split payments (gift card + card) are kept
but flagged: the card was charged less than the subtotal, so they can only
ever be soft-suggested against a statement, never exact-amount matched.

Item lines are kept whole — every row's product name with its own Total
Amount — so enrichment and later tooling can see what was actually bought,
not a truncated name list.

Pure functions — no DB access — mirroring ``import_service.parse_*``.
"""
import csv
import io
import re
from datetime import date as _date
from decimal import Decimal

from app.schemas.amazon import AmazonCharge

# A charge must carry at least these to be matchable. "Shipment Item
# Subtotal" is what the statement charged, "Payment Method Type" tells us
# which card (and whether it was a split), and Ship Date anchors the
# statement-window search. "Total Amount" stays required: it is the fallback
# amount source and what item tracking is recorded against.
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


def _parse_money(value: str) -> Decimal | None:
    try:
        amount = Decimal((value or "").strip() or "0")
    except Exception:
        return None
    return amount if amount > 0 else None


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
    groups: dict[tuple, list[dict]] = {}

    for row in reader:
        row = {(k or "").lower().strip(): (v or "") for k, v in row.items()}

        order_id = row.get("order id", "").strip()
        if not order_id:
            continue

        total = _parse_money(row.get("total amount", ""))
        if total is None:
            continue

        ship_date = _parse_date(row.get("ship date", "")) or _parse_date(row.get("order date", ""))
        if ship_date is None:
            continue

        last4, _ = _parse_payment(row.get("payment method type", ""))
        if last4 is None:
            # Paid without a card (gift card / rewards / blank) — it will never
            # appear on a card statement, so there is nothing to match against.
            continue

        tracking = row.get("carrier name & tracking number", "").strip()
        if tracking in ("Not Available", "Not Applicable"):
            tracking = ""

        groups.setdefault((order_id, tracking, ship_date), []).append(row)

    charges: list[AmazonCharge] = []
    for (order_id, tracking, ship_date), rows in groups.items():
        first = rows[0]
        last4, is_split = _parse_payment(first.get("payment method type", ""))

        # "Shipped and Shipped" rows name two tracking numbers: the item
        # crossed two parcels, so the row total may not equal any single card
        # charge — report-only, like a split payment.
        if " and " in tracking:
            is_split = True

        # Amount source, in order of trust:
        # 1. the shipment's Shipment Item Subtotal (repeated on every item
        #    row of the shipment — take it once, summing would double-count);
        # 2. the summed per-item Total Amounts (exports without the subtotal
        #    column, or digital rows that carry none).
        subtotal = None
        total_sum = Decimal("0")
        for row in rows:
            if subtotal is None:
                subtotal = _parse_money(row.get("shipment item subtotal", ""))
            total_sum += Decimal((row.get("total amount") or "0").strip() or "0")
        amount = (subtotal if subtotal is not None else total_sum).quantize(Decimal("0.01"))

        # A shipment paid by two cards is rare; if it happens, refuse to
        # exact-match it rather than guess which card carried the balance.
        for row in rows[1:]:
            row_last4, row_split = _parse_payment(row.get("payment method type", ""))
            if row_last4 != last4 or row_split:
                is_split = True

        # Every line item, with its own Total Amount — kept whole and in
        # order for enrichment and tracking (no capping, no collapsing).
        items: list[dict] = []
        for row in rows:
            name = row.get("product name", "").strip()
            if not name:
                continue
            items.append({"name": name, "amount": (row.get("total amount") or "").strip()})

        charges.append(AmazonCharge(
            order_id=order_id,
            tracking=tracking,
            ship_date=ship_date,
            order_date=_parse_date(first.get("order date", "")),
            amount=amount,
            currency=(first.get("currency", "").strip().upper() or "USD"),
            card_last4=last4,
            is_split_payment=is_split,
            items=items,
        ))

    return charges
