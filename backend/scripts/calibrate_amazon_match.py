#!/usr/bin/env python3
"""Calibrate Amazon charge matching against a real Order History export.

Two modes:

1. Perfect statement (default): one synthetic debit per parsed charge, at the
   charge's own amount, posted ship+1 (split payments seeded 20 % low). Every
   non-split charge then has its exact payment inside the match window, so
   anything that fails to auto-link — or links to a different purchase's
   payment — is a heuristic gap, not bad luck.

2. --statement <csv>: seed the match against a REAL statement dump instead
   (rows of date, amount, description; header optional). This is the true
   backtest — but only when export and statement cover the same purchases. A
   May export cannot reconcile against a June statement: there is nothing to
   pair, and the report will say so.

Usage (from backend/):
    python scripts/calibrate_amazon_match.py "/path/to/Order History.csv"
    python scripts/calibrate_amazon_match.py export.csv --statement statement.csv
"""
import asyncio
import csv
import io
import os
import sys
import time
import uuid
from datetime import date as _date, timedelta
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


def _read_statement(path: str) -> list[tuple[_date, Decimal, str]]:
    """Read a statement dump of (date, amount, description) rows."""
    with open(path, encoding="utf-8-sig") as f:
        rows = []
        for line in f:
            parts = [p.strip().strip('"') for p in line.rstrip("\r\n").split(",")]
            if len(parts) < 3:
                continue
            try:
                day = _date.fromisoformat(parts[0])
                amount = Decimal(parts[1])
            except ValueError:
                continue  # header or malformed line
            if amount > 0:
                rows.append((day, amount, parts[2]))
        return rows


async def main(csv_path: str, statement_path: str | None = None) -> None:
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

        seeded: dict[uuid.UUID, tuple] = {}  # txn id -> charge key (perfect mode)
        auto_expect = 0

        if statement_path:
            # Real-statement backtest: seed exactly what the bank saw.
            for day, amount, description in _read_statement(statement_path):
                txn = Transaction(
                    id=uuid.uuid4(), user_id=user_id, workspace_id=workspace_id,
                    account_id=account.id, description=description,
                    amount=amount, date=day,
                    type="debit", currency="USD", source="csv",
                )
                session.add(txn)
            print(f"statement debits seeded: {len(session.new)}")
        else:
            # Perfect statement: one payment per charge, posted ship+1.
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
        wrong = None
        if seeded:
            purchases = (await session.execute(select(AmazonPurchase))).scalars().all()
            wrong = sum(
                1 for p in purchases
                if p.transaction_id is not None and seeded[p.transaction_id] != (p.order_id, p.tracking, p.ship_date)
            )

        # Idempotency: a second import of the same file must re-link nothing.
        second = await purchase_match_service.match_purchases(
            session, workspace_id, charges, apply=True,
        )

    linked = report.auto_matched
    print(f"export rows:        {content.count(chr(10).encode()) - 1}")
    print(f"charges parsed:     {report.charges_parsed}  (parse {parse_s:.2f}s)")
    print(f"auto-linked:        {linked}" + (f"  (expected {auto_expect}: non-split charges)" if seeded else ""))
    print(f"suggestions:        {report.suggestions}")
    print(f"unmatched:          {report.unmatched}")
    if wrong is not None:
        print(f"mispaired links:    {wrong}")
    print(f"reimport skipped:   {second.skipped_existing}  (first run linked {linked})")
    print(f"match wall time:    {match_s:.2f}s  (match+persist, in-memory sqlite)")
    if seeded:
        ok = wrong == 0 and report.auto_matched == auto_expect and second.skipped_existing == report.auto_matched
        print("RESULT:", "PASS — perfect statement fully reconciled" if ok else "GAPS FOUND — see counts above")
    else:
        print("RESULT:", "backtest only — compare against the perfect-statement run")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flag = "--statement" in sys.argv
    path = args[0] if args else "Order History.csv"
    stmt = args[1] if flag and len(args) > 1 else None
    if flag and not stmt:
        sys.exit("usage: calibrate_amazon_match.py <export.csv> --statement <statement.csv>")
    asyncio.run(main(path, stmt))
