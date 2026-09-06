"""Schemas for the Amazon "Order History" import + purchase matching."""
import uuid
from datetime import date
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel


class AmazonCharge(BaseModel):
    """One charge candidate parsed from an Amazon Order History export.

    One charge = one shipment (Amazon charges the card when a shipment
    leaves), which is the unit that appears on the credit-card statement.
    """

    order_id: str
    tracking: str = ""  # raw Carrier Name & Tracking Number; "" when absent
    ship_date: date
    order_date: Optional[date] = None
    amount: Decimal  # shipment's Shipment Item Subtotal (or summed Total
                    # Amounts when absent); pre-gift-card-split
    currency: str = "USD"
    card_last4: Optional[str] = None
    is_split_payment: bool = False  # gift card covered part of the charge
    items: list[dict] = []  # every line item as {"name", "amount"}


class AmazonMatchEntry(BaseModel):
    """One charge paired with a card transaction (auto-linked or suggested)."""

    order_id: str
    tracking: str = ""
    ship_date: date
    amount: Decimal
    tier: Literal["auto", "suggest"]
    transaction_id: Optional[uuid.UUID] = None
    transaction_description: Optional[str] = None
    reason: Optional[str] = None  # why a suggestion is only a suggestion


class AmazonMatchReport(BaseModel):
    format: str = "amazon_order_history"
    charges_parsed: int = 0
    auto_matched: int = 0
    suggestions: int = 0
    unmatched: int = 0
    skipped_existing: int = 0  # already linked by an earlier import
    matches: list[AmazonMatchEntry] = []
    suggested: list[AmazonMatchEntry] = []
