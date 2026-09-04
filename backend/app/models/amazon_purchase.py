import uuid
from datetime import date as _date, datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    JSON,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class AmazonPurchase(Base):
    """One parsed charge from an Amazon "Order History" export.

    A purchase is one *shipment* — the unit Amazon actually charged the card
    for, and therefore the unit that must be reconciled against a credit-card
    transaction. Rows are kept after matching so the data stays queryable
    (agent context, future MCP tools) and re-imports stay idempotent.
    """

    __tablename__ = "amazon_purchases"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE")
    )
    # The card transaction this purchase was matched to. Null = unmatched or
    # only soft-suggested; ON DELETE SET NULL keeps a deleted transaction from
    # taking the parsed purchase down with it.
    transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("transactions.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    order_id: Mapped[str] = mapped_column(String(32))
    # Raw "Carrier Name & Tracking Number" value; "" when the export has none.
    # Part of the charge identity alongside order_id + ship_date, because one
    # order can be charged as several shipments on the same day.
    tracking: Mapped[str] = mapped_column(String(64), default="")
    ship_date: Mapped[_date] = mapped_column(Date)
    order_date: Mapped[_date | None] = mapped_column(Date, nullable=True)
    # Sum of the per-row "Total Amount" over the shipment's items — what the
    # card *should* show, before any gift-card split payment reduced it.
    amount: Mapped[Decimal] = mapped_column(Numeric(precision=15, scale=2))
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    card_last4: Mapped[str | None] = mapped_column(String(4), nullable=True)
    # True when payment was "Gift Certificate/Card and <card>": only part of
    # the charge went to the card, so exact-amount matching must not fire.
    is_split_payment: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # Product names, de-duplicated and capped — the enrichment payload that
    # rules (via notes) and agent context can use to classify a charge.
    items: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "order_id", "tracking", "ship_date",
            name="uq_amazon_purchases_charge",
        ),
    )
