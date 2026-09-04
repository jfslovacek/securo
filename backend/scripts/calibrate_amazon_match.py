#!/usr/bin/env python3
"""Calibrate Amazon charge matching against a real Order History export.

Feeds the production parser + matcher a "perfect statement": one synthetic
card transaction per parsed charge, posted one day after shipping and
described "AMZN Mktp US*". Gift-card split payments get their transaction
seeded 20 % low, like the real thing (the card saw less than the export
total). Under those conditions every non-split charge has its exact payment
inside the match window, so anything that fails to auto-link — or links to a
different purchase's payment — is a heuristic gap, not bad luck.

Reports: charges parsed (vs export rows), auto-linked, mispaired links, and
wall timings at real-export scale (2k+ rows).

Usage (from backend/):
    python scripts/calibrate_amazon_match.py "/path/to/Order History.csv"
"""
import asyncio
import os
import sys
import time
import uuid
from datetime import timedelta
from decimal import Decimal

# Allow running from repo root or backend root
_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
sys.path.insert(0, os.path.dirname(_BACKEND))
sys.path.insert(0, _BACKEND)

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.core.database import Base  # noqa: E402
from app.models.account import Account  # noqa: E402
from app.models.amazon_purchase import AmazonPurchase  # noqa: E402
from app.models.transaction import Transaction  # noqa: E402
from app.services import amazon_import_service, purchase_match_service  # noqa: E402


async def main(csv_path: str) -> None:
    with open(csv_path, "rb") as f:
        content = f.read()

    t0 = time.perf_counter()
    charges = amazon_import_service.parse_order_history(content)
    parse_s = time.perf_counter() - t0
    if not charges:
        print("No charges parsed — is this an Amazon Order History export?")
        return

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        workspace_id = uuid.uuid4()
        user_id = uuid.uuid4()
        account = Account(
            id=uuid.uuid4(), user_id=user_id, workspace_id=workspace_id,
            name="Calibration Card", type="credit_card", masked_number=None,
            balance=Decimal("0"), currency="USD",
        )
        session.add(account)

        # Seed the perfect statement: one payment per charge, posted ship+1.
        seeded: dict[uuid.UUID, tuple] = {}  # txn id -> charge key
        auto_expect = 0
        for c in charges:
            if not c.is_split_payment:
                auto_expect += 1
                amount = c.amount
            else:
                amount = (c.amount * Decimal("0.8")).quantize(Decimal("0.01"))
            txn = Transaction(
                id=uuid.uuid4(), user_id=user_id, workspace_id=workspace_id,
                account_id=account.id, description="AMZN Mktp US*4021",
                amount=amount, date=c.ship_date + timedelta(days=1),
                type="debit", currency="USD", source="csv",
            )
            seeded[txn.id] = (c.order_id, c.tracking, c.ship_date)
            session.add(txn)
        await session.commit()

        t1 = time.perf_counter()
        report = await purchase_match_service.match_purchases(
            session, workspace_id, charges, apply=True,
        )
        match_s = time.perf_counter() - t1

        # Verify every stored link points at that charge's own seeded payment.
        purchases = (await session.execute(select(AmazonPurchase))).scalars().all()
        wrong = sum(
            1 for p in purchases
            if p.transaction_id is not None and seeded[p.transaction_id] != (p.order_id, p.tracking, p.ship_date)
        )

        # Idempotency: a second import of the same file must re-link nothing.
        second = await purchase_match_service.match_purchases(
            session, workspace_id, charges, apply=True,
        )

    linked = sum(1 for p in purchases if p.transaction_id is not None)
    print(f"export rows:        {content.count(chr(10).encode()) - 1}")
    print(f"charges parsed:     {report.charges_parsed}  (parse {parse_s:.2f}s)")
    print(f"auto-linked:        {report.auto_matched}  (expected {auto_expect}: non-split charges)")
    print(f"suggestions:        {report.suggestions}")
    print(f"unmatched:          {report.unmatched}")
    print(f"mispaired links:    {wrong}")
    print(f"purchases stored:   {len(purchases)}  (linked {linked})")
    print(f"reimport skipped:   {second.skipped_existing}  (first run linked {report.auto_matched})")
    print(f"match wall time:    {match_s:.2f}s  (match+persist, in-memory sqlite)")
    ok = wrong == 0 and report.auto_matched == auto_expect and second.skipped_existing == report.auto_matched
    print("RESULT:", "PASS — perfect statement fully reconciled" if ok else "GAPS FOUND — see counts above")


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "Order History.csv"
    asyncio.run(main(path))
