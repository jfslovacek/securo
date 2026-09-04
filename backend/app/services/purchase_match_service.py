"""Match Amazon Order History charges to the credit-card charges that paid them.

A parsed charge (one shipment) is reconciled against existing debit
transactions on credit-card accounts, mirroring `recurring_match_service`:
matches are one-to-one and only the high-confidence (exact-amount) tier
auto-links; softer matches are reported as suggestions, never persisted as
links. Matching is scoped to debit transactions whose description still names
Amazon (AMZN/AMAZON), so a coincidental $25.00 Uber Eats order never swallows
an Amazon charge — and the enriched `notes` a successful match writes back
("Amazon #<order>: <items>") is what lets rules and agent context classify the
purchase by item, not just by merchant.

An import is idempotent: charges already linked by an earlier import are
skipped, and re-importing the same export never duplicates a purchase row or
its enrichment text.
"""
import logging
import uuid
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.amazon_purchase import AmazonPurchase
from app.models.transaction import Transaction
from app.schemas.amazon import AmazonCharge, AmazonMatchEntry, AmazonMatchReport
from app.services.rule_engine import merge_notes

logger = logging.getLogger(__name__)

# Amazon charges the card when a shipment leaves; statement postings lag by
# roughly a day, so candidates may sit anywhere from the ship date to a few
# days after it.
MATCH_WINDOW_DAYS = 5

# Exact-amount tier. Statement rounding/gift-card splits put charges outside
# this band; those fall through to the suggestion tier.
AMOUNT_TOLERANCE = Decimal("0.01")

# Suggestion tier: a card charge this far below the export total is plausibly
# the same purchase (gift-card split, partial refund). Reported, never linked.
SUGGEST_MIN_RATIO = Decimal("0.5")

# Normalized (uppercased) description fragments Amazon statements use.
_DESCRIPTORS = ("AMZN", "AMAZON")

# Enrichment budget inside Transaction.notes (String(1000)).
_NOTES_MAX_CHARS = 700


def _has_amazon_descriptor(description: str | None) -> bool:
    upper = (description or "").upper()
    return any(token in upper for token in _DESCRIPTORS)


def _purchase_fields(charge: AmazonCharge) -> dict:
    """Shared constructor payload for a charge's storage row.

    The parsed data is stored whole (amount, items, split flag) — not just the
    link — so re-imports stay idempotent and purchases stay queryable even
    when nothing matched them.
    """
    return dict(
        order_id=charge.order_id,
        tracking=charge.tracking,
        ship_date=charge.ship_date,
        order_date=charge.order_date,
        amount=charge.amount,
        currency=charge.currency,
        card_last4=charge.card_last4,
        is_split_payment=charge.is_split_payment,
        items=charge.items,
    )


async def match_purchases(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    charges: list[AmazonCharge],
    *,
    account_id: uuid.UUID | None = None,
    apply: bool = False,
) -> AmazonMatchReport:
    """Pair parsed charges with card transactions; optionally persist links.

    With `apply=False` nothing is written — the returned report previews what
    an import would do. With `apply=True` matched/unmatched purchases are
    stored in `amazon_purchases`, matches gain their transaction link, and a
    matched transaction's notes are enriched with the item names.
    """
    report = AmazonMatchReport(charges_parsed=len(charges))
    if not charges:
        return report

    # ── Target accounts ────────────────────────────────────────────────────
    if account_id is not None:
        account = await session.get(Account, account_id)
        if account is None or account.workspace_id != workspace_id:
            raise ValueError("Account not found")
        accounts = [account]
    else:
        result = await session.execute(
            select(Account).where(
                Account.workspace_id == workspace_id,
                Account.type == "credit_card",
                Account.is_closed == False,  # noqa: E712
            )
        )
        accounts = list(result.scalars().all())
    if not accounts:
        raise ValueError("No open credit-card account to match against")

    # ── Candidate transactions (one query, then bucketed in Python) ───────
    min_ship = min(c.ship_date for c in charges)
    max_ship = max(c.ship_date for c in charges)
    window_end = max_ship + timedelta(days=MATCH_WINDOW_DAYS)
    account_ids = [a.id for a in accounts]

    result = await session.execute(
        select(Transaction).where(
            Transaction.account_id.in_(account_ids),
            Transaction.type == "debit",
            Transaction.is_ignored == False,  # noqa: E712
            Transaction.date >= min_ship,
            Transaction.date <= window_end,
        )
    )
    candidates = list(result.scalars().all())

    # ── Idempotency + one-to-one bookkeeping ─────────────────────────────
    result = await session.execute(
        select(AmazonPurchase).where(AmazonPurchase.workspace_id == workspace_id)
    )
    existing_by_key = {(p.order_id, p.tracking, p.ship_date): p for p in result.scalars()}

    linked_ids = {p.transaction_id for p in existing_by_key.values() if p.transaction_id}
    alive_linked: set[uuid.UUID] = set()
    if linked_ids:
        result = await session.execute(
            select(Transaction.id).where(Transaction.id.in_(linked_ids))
        )
        alive_linked = set(result.scalars().all())
    used_txn_ids: set[uuid.UUID] = set(alive_linked)

    # Bucket by (account, amount) for the exact tier and index candidates by
    # (account, date) for the suggestion scan. A real export yields ~1,500
    # charges and a card account thousands of debits; scanning every candidate
    # per charge is quadratic, while six date-key lookups per charge are not.
    exact_buckets: dict[tuple, list[Transaction]] = {}
    by_account_date: dict[tuple, list[Transaction]] = {}
    for txn in candidates:
        if txn.id in used_txn_ids or not _has_amazon_descriptor(txn.description):
            continue
        exact_buckets.setdefault((txn.account_id, txn.amount), []).append(txn)
        by_account_date.setdefault((txn.account_id, txn.date), []).append(txn)

    def window_candidates(charge: AmazonCharge):
        """Amazon-text debits inside the ship-date window, earliest date first.

        The (account, date) index already orders the scan by date, so the
        first hit is also the earliest posting — the pairing heuristic picks
        the charge's own payment before a later twin's can be stolen.
        """
        dates = [charge.ship_date + timedelta(days=d) for d in range(MATCH_WINDOW_DAYS + 1)]
        if charge.card_last4:
            scoped = [a for a in accounts if a.masked_number == charge.card_last4] or accounts
        else:
            scoped = accounts
        for account in scoped:
            for day in dates:
                for txn in by_account_date.get((account.id, day), ()):
                    if txn.id in used_txn_ids:
                        continue
                    yield txn

    # ── Match ────────────────────────────────────────────────────────────
    # Ship-date order, not file order: real exports repeat amounts ($10.81
    # appears 27 times), and a charge's own payment posts no later than a
    # later twin's. Processing earlier shipments first lets each one take its
    # own transaction before a later charge can scan past it — file order
    # could match the twins to each other's payments.
    charges = sorted(charges, key=lambda c: c.ship_date)
    matched_pairs: list[tuple[AmazonCharge, Transaction]] = []

    for charge in charges:
        key = (charge.order_id, charge.tracking, charge.ship_date)
        existing = existing_by_key.get(key)
        if existing is not None and existing.transaction_id in alive_linked:
            report.skipped_existing += 1
            continue

        hit = None
        # Split payments never auto-link: the card saw LESS than the export
        # total (gift-card part unknown), so an exact-amount hit inside the
        # window is more likely another purchase's payment than this charge's.
        if not charge.is_split_payment:
            for account in (accounts if not charge.card_last4 else
                            [a for a in accounts if a.masked_number == charge.card_last4] or accounts):
                for delta in (Decimal("0"), Decimal("0.01"), Decimal("-0.01")):
                    for txn in exact_buckets.get((account.id, charge.amount + delta), ()):
                        if txn.id in used_txn_ids:
                            continue
                        if not (charge.ship_date <= txn.date
                                and txn.date <= charge.ship_date + timedelta(days=MATCH_WINDOW_DAYS)):
                            continue
                        # Earliest posting date wins; uuid is not orderable, so
                        # same-date ties keep scan order (stable per run).
                        if hit is None or txn.date < hit.date:
                            hit = txn
                if hit is not None:
                    break

        if hit is not None:
            used_txn_ids.add(hit.id)
            matched_pairs.append((charge, hit))
            report.auto_matched += 1
            report.matches.append(AmazonMatchEntry(
                order_id=charge.order_id,
                tracking=charge.tracking,
                ship_date=charge.ship_date,
                amount=charge.amount,
                tier="auto",
                transaction_id=hit.id,
                transaction_description=hit.description,
            ))
            continue

        # Suggestion tier: report-only. Split payments charge the card less
        # than the export total, so anything in [50 %, 100 %) of the charge
        # with an Amazon descriptor and a window-plausible date is worth
        # surfacing for manual confirmation — nothing more.
        low = (charge.amount * SUGGEST_MIN_RATIO).quantize(Decimal("0.01"))
        suggestion = None
        for txn in window_candidates(charge):
            if low <= txn.amount < charge.amount - AMOUNT_TOLERANCE:
                suggestion = txn
                break
        if suggestion is not None:
            report.suggestions += 1
            report.suggested.append(AmazonMatchEntry(
                order_id=charge.order_id,
                tracking=charge.tracking,
                ship_date=charge.ship_date,
                amount=charge.amount,
                tier="suggest",
                transaction_id=suggestion.id,
                transaction_description=suggestion.description,
                reason=("split_payment" if charge.is_split_payment else "amount_tolerance"),
            ))
        else:
            report.unmatched += 1

    # ── Persist (apply only) ─────────────────────────────────────────────
    if apply:
        matched_by_key = {(c.order_id, c.tracking, c.ship_date): t for c, t in matched_pairs}
        for charge in charges:
            key = (charge.order_id, charge.tracking, charge.ship_date)
            if key in matched_by_key:
                txn = matched_by_key[key]
                purchase = existing_by_key.get(key)
                if purchase is not None and purchase.transaction_id == txn.id:
                    continue  # already linked to the same charge
                if purchase is None:
                    purchase = AmazonPurchase(
                        workspace_id=workspace_id,
                        account_id=txn.account_id,
                        **_purchase_fields(charge),
                    )
                    session.add(purchase)
                purchase.transaction_id = txn.id
                # Enrich for rules (`notes` is a rule-matchable field) and
                # agent context. merge_notes keeps this idempotent.
                summary = "; ".join(charge.items)[:_NOTES_MAX_CHARS]
                note = f"Amazon #{charge.order_id}: {summary}" if summary \
                    else f"Amazon #{charge.order_id}"
                txn.notes = merge_notes(txn.notes, note)
            elif key not in existing_by_key:
                # Record unmatched charges too: re-imports stay idempotent and
                # the data stays queryable for later suggestion passes. The
                # owning account is the one named by the export's card last-4
                # (when known), else the first open card account.
                owner = next(
                    (a for a in accounts if charge.card_last4
                     and a.masked_number == charge.card_last4),
                    accounts[0],
                )
                session.add(AmazonPurchase(
                    workspace_id=workspace_id,
                    account_id=owner.id,
                    **_purchase_fields(charge),
                ))

        await session.commit()

    logger.info(
        "Amazon match: %d charges, %d auto-linked, %d suggested, %d unmatched, %d already linked",
        report.charges_parsed, report.auto_matched, report.suggestions,
        report.unmatched, report.skipped_existing,
    )
    return report
