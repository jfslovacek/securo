"""store Amazon Order History purchases as matchable charge records

Revision ID: 086
Revises: 085
Create Date: 2026-09-03

An Amazon "Order History" export lists what was bought, not what was charged:
one row per shipment item, with split payments and multi-shipment orders
scrambling any naive one-row-per-charge reading. The parser rebuilds charge
candidates (one per shipment); this table stores them so matching is
idempotent, links survive re-imports, and the item names stay queryable for
rules and agent context. See docs/amazon-order-history-matching.md.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "086"
down_revision: Union[str, None] = "085"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "amazon_purchases",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "account_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "transaction_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("transactions.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("order_id", sa.String(32), nullable=False),
        sa.Column("tracking", sa.String(64), nullable=False, server_default=""),
        sa.Column("ship_date", sa.Date(), nullable=False),
        sa.Column("order_date", sa.Date(), nullable=True),
        sa.Column("amount", sa.Numeric(precision=15, scale=2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False, server_default="USD"),
        sa.Column("card_last4", sa.String(4), nullable=True),
        sa.Column("is_split_payment", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("items", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "workspace_id", "order_id", "tracking", "ship_date",
            name="uq_amazon_purchases_charge",
        ),
    )
    op.create_index(
        "ix_amazon_purchases_workspace_id", "amazon_purchases", ["workspace_id"],
    )
    op.create_index(
        "ix_amazon_purchases_transaction_id", "amazon_purchases", ["transaction_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_amazon_purchases_transaction_id", table_name="amazon_purchases")
    op.drop_index("ix_amazon_purchases_workspace_id", table_name="amazon_purchases")
    op.drop_table("amazon_purchases")
