"""Tests for matching parsed Amazon charges to credit-card transactions.

The fixture file drives the parser tests; here charges are hand-built so each
test isolates one matcher rule: window, descriptor gate, amount tiers,
one-to-one linking, and idempotent re-imports.
"""
import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.models.account import Account
from app.models.amazon_purchase import AmazonPurchase
from app.models.transaction import Transaction
from app.schemas.amazon import AmazonCharge
from app.services import purchase_match_service


def _charge(amount, ship_day, *, order_id="111-0000000-0000000", tracking="TBA1234567890",
            last4="9371", split=False):
    return AmazonCharge(
        order_id=order_id,
        tracking=tracking,
        ship_date=date(2026, 5, ship_day),
        order_date=date(2026, 5, ship_day - 1),
        amount=Decimal(amount),
        card_last4=last4,
        is_split_payment=split,
        items=["Widget A", "Widget B"],
    )


async def _card(session, user, ws, *, masked="9371", name="Chase Freedom"):
    account = Account(
        id=uuid.uuid4(), user_id=user.id, workspace_id=ws.id, name=name,
        type="credit_card", masked_number=masked, balance=Decimal("0"), currency="USD",
    )
    session.add(account)
    await session.commit()
    return account


async def _txn(session, user, ws, account, description, amount, day, *, notes=None):
    txn = Transaction(
        id=uuid.uuid4(), user_id=user.id, workspace_id=ws.id, account_id=account.id,
        description=description, amount=Decimal(amount), date=date(2026, 5, day),
        type="debit", currency="USD", source="csv", notes=notes,
    )
    session.add(txn)
    await session.commit()
    return txn


@pytest.mark.asyncio
async def test_preview_links_in_memory_without_writing(session, test_user, test_workspace):
    acct = await _card(session, test_user, test_workspace)
    txn = await _txn(session, test_user, test_workspace, acct, "AMZN Mktp US", "28.40", 9)

    report = await purchase_match_service.match_purchases(
        session, test_workspace.id, [_charge("28.40", 8)], apply=False,
    )

    assert report.auto_matched == 1
    assert report.matches[0].transaction_id == txn.id
    rows = await session.execute(
        select(func.count()).select_from(AmazonPurchase).where(
            AmazonPurchase.workspace_id == test_workspace.id
        )
    )
    assert rows.scalar() == 0  # preview persists nothing


@pytest.mark.asyncio
async def test_apply_links_and_enriches_notes(session, test_user, test_workspace):
    acct = await _card(session, test_user, test_workspace)
    txn = await _txn(session, test_user, test_workspace, acct, "AMZN Mktp US", "28.40", 9)

    report = await purchase_match_service.match_purchases(
        session, test_workspace.id, [_charge("28.40", 8)], apply=True,
    )

    assert report.auto_matched == 1 and report.unmatched == 0
    purchase = (await session.execute(select(AmazonPurchase))).scalars().one()
    assert purchase.transaction_id == txn.id
    # Item names landed where rules and agent context can read them.
    assert "Widget A" in txn.notes and "Widget B" in txn.notes


@pytest.mark.asyncio
async def test_descriptor_gate_blocks_non_amazon_debits(session, test_user, test_workspace):
    acct = await _card(session, test_user, test_workspace)
    await _txn(session, test_user, test_workspace, acct, "TARGET STORE", "28.40", 9)

    report = await purchase_match_service.match_purchases(
        session, test_workspace.id, [_charge("28.40", 8)], apply=True,
    )

    assert report.auto_matched == 0 and report.suggestions == 0
    assert report.unmatched == 1


@pytest.mark.asyncio
async def test_charge_outside_window_stays_unmatched(session, test_user, test_workspace):
    acct = await _card(session, test_user, test_workspace)
    # Charged 20 days after shipping — implausible, so not linked.
    await _txn(session, test_user, test_workspace, acct, "AMZN Mktp US", "28.40", 28)

    report = await purchase_match_service.match_purchases(
        session, test_workspace.id, [_charge("28.40", 8)], apply=False,
    )
    assert report.unmatched == 1


@pytest.mark.asyncio
async def test_one_to_one_second_charge_cannot_reuse_transaction(session, test_user, test_workspace):
    acct = await _card(session, test_user, test_workspace)
    await _txn(session, test_user, test_workspace, acct, "AMZN Mktp US", "28.40", 9)

    charges = [
        _charge("28.40", 8, order_id="111-1", tracking="TBA1"),
        _charge("28.40", 8, order_id="111-2", tracking="TBA2"),
    ]
    report = await purchase_match_service.match_purchases(
        session, test_workspace.id, charges, apply=False,
    )

    assert report.auto_matched == 1
    assert report.unmatched == 1


@pytest.mark.asyncio
async def test_split_payment_is_suggested_not_linked(session, test_user, test_workspace):
    acct = await _card(session, test_user, test_workspace)
    # Gift card + Visa: the export shows 27.06, the card saw 25.99.
    txn = await _txn(session, test_user, test_workspace, acct, "AMZN Mktp US", "25.99", 9)

    report = await purchase_match_service.match_purchases(
        session, test_workspace.id, [_charge("27.06", 8, split=True)], apply=True,
    )

    assert report.auto_matched == 0 and report.suggestions == 1
    assert report.suggested[0].reason == "split_payment"
    purchase = (await session.execute(select(AmazonPurchase))).scalars().one()
    assert purchase.transaction_id is None  # suggestions never auto-link
    assert txn.notes is None


@pytest.mark.asyncio
async def test_reimport_is_idempotent(session, test_user, test_workspace):
    acct = await _card(session, test_user, test_workspace)
    txn = await _txn(
        session, test_user, test_workspace, acct, "AMZN Mktp US", "28.40", 9,
        notes="already a note",
    )

    first = await purchase_match_service.match_purchases(
        session, test_workspace.id, [_charge("28.40", 8)], apply=True,
    )
    second = await purchase_match_service.match_purchases(
        session, test_workspace.id, [_charge("28.40", 8)], apply=True,
    )

    assert first.auto_matched == 1
    assert second.skipped_existing == 1 and second.auto_matched == 0

    rows = (await session.execute(select(AmazonPurchase))).scalars().all()
    assert len(rows) == 1
    assert txn.notes.count("Amazon #") == 1


@pytest.mark.asyncio
async def test_last4_routes_to_the_matching_card_account(session, test_user, test_workspace):
    await _card(session, test_user, test_workspace, masked="9371", name="Chase")
    capital = await _card(session, test_user, test_workspace, masked="7944", name="Capital One")
    txn = await _txn(session, test_user, test_workspace, capital, "AMZN Mktp US", "28.40", 9)

    report = await purchase_match_service.match_purchases(
        session, test_workspace.id, [_charge("28.40", 8, last4="7944")], apply=True,
    )

    assert report.auto_matched == 1
    assert report.matches[0].transaction_id == txn.id


@pytest.mark.asyncio
async def test_empty_export_reports_zero(session, test_user, test_workspace):
    report = await purchase_match_service.match_purchases(
        session, test_workspace.id, [], apply=True,
    )
    assert report.charges_parsed == 0
