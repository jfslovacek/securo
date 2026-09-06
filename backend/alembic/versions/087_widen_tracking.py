"""widen amazon_purchases.tracking for compound multi-parcel strings

Revision ID: 087
Revises: 086
Create Date: 2026-09-05

Real exports put every tracking number of a multi-parcel shipment in ONE
"Carrier Name & Tracking Number" cell — "RABBIT(...) and RABBIT(...) and
..." with up to five numbers, measured at 160 characters on a real export.
The raw string is part of the charge-identity key (and the parser's
report-only compound flag keys off it), so it is stored verbatim, never
truncated: varchar(64) overflowed and killed the first real import.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "087"
down_revision: Union[str, None] = "086"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "amazon_purchases", "tracking",
        existing_type=sa.String(length=64), type_=sa.String(length=255),
        existing_nullable=False, existing_server_default="",
    )


def downgrade() -> None:
    op.alter_column(
        "amazon_purchases", "tracking",
        existing_type=sa.String(length=255), type_=sa.String(length=64),
        existing_nullable=False, existing_server_default="",
    )
