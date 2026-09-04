# Amazon Order History import & purchase→transaction matching

Status: draft (PR candidate) · Scope: backend only

## Problem

Users who pay for Amazon purchases with a credit card end up with two views of the
same money:

1. **Card charges** — imported or synced into a credit-card account as opaque rows
   (`AMZN.MKTP.US 402-8841231-9284755`, `AMAZON Mktp WA...`), usually one charge per
   *shipment*, sometimes partially paid with gift-card balance.
2. **Amazon's own order export** — the "Order History" CSV from a *Request Your Data*
   privacy request, which lists every item with product names, dates, and amounts.

The card charges carry no item information, so classification rules and the AI
agent features (which read transactions through `context_service` / MCP tools) can
only ever see "AMZN Mktp". This feature parses the Amazon export, matches each
purchase to the card transaction that paid for it, and enriches the matched
transaction with item names — making purchase data available to rules (which can
match on `notes`) and to agent context, without double-counting spend.

## What the real export looks like (measured on a 2,132-row / 1,398-order file)

The export is **one row per shipment item**, not one row per order or per charge:

- 2,132 rows → 1,398 distinct `Order ID`s → ~1,579 (order, tracking) groups.
- A single order split into 4 shipments appears as 4+ rows with **different**
  `Carrier Name & Tracking Number` (TBA…) values — each was a separate card charge.
- `Total Amount` is **per row (item)**, including item tax and any shipping charged.
  The card charge for a shipment is the sum of `Total Amount` over the rows sharing
  one tracking number. (`Shipment Item Subtotal` is repeated per shipment and would
  double-count if summed.)
- `Payment Method Type` carries brand + card last-4: `Visa - 9371`, and split
  payments like `Gift Certificate/Card and Visa - 7944` (gift card covered part of
  the cost, so **the card charge is lower than the row total** — ~2% of rows here).
- `Ship Date`/`Order Date` are ISO timestamps (`2022-05-26T02:17:15Z`). Amazon
  charges when a shipment leaves the warehouse, so charge date ≈ ship date.
- Some rows have `Not Available` tracking (42 here) or no card at all (gift-card-
  only purchases — those never hit a card statement and are dropped).

## Design

### Parse (new `amazon_import_service.py`)

`detect_format()` recognizes the export by its required headers
(`Order ID`, `Total Amount`, `Product Name`, `Ship Date`, `Payment Method Type`).
`parse_order_history()` returns **charge candidates** (`AmazonCharge`):

- grouping key = `(Order ID, tracking, ship-date)` — tracking is the shipment key;
  rows without tracking group by ship date instead (rare, documented risk of
  merging two same-day shipments of one order);
- `amount` = sum of per-row `Total Amount` (2dp), `currency` from `Currency`;
- `card_last4` / brand parsed from `Payment Method Type`; rows paying **only** with
  gift card (no trailing last-4) are dropped — they cannot match a card charge;
- `is_split_payment` when the payment string contains `Gift Certificate/Card and` —
  the true card charge is smaller than `amount`, so these never auto-match;
- `items` = de-duplicated product names (≤12, truncated) for enrichment.

### Match (new `purchase_match_service.py`)

Modeled on `recurring_match_service` (one-to-one, exact-amount auto tier, soft
tier reported only):

- **Accounts**: explicit `account_id` param wins; otherwise every open
  `credit_card` account in the workspace. When a charge carries a card last-4,
  accounts whose `masked_number` matches are preferred (falls back to all).
- **Candidates**: `debit` transactions, not ignored, dated within
  `[ship_date, ship_date + 5 days]`.
- **Descriptor gate**: transaction description must contain `AMZN` or `AMAZON`
  (normalized) before any amount comparison — keeps non-Amazon debits from
  linking on coincidental amounts.
- **Tier A (auto-link)**: `|txn.amount − charge.amount| ≤ $0.01`. One-to-one: a
  transaction links to at most one purchase (already-linked transactions are
  excluded, mirroring recurring-bill matching).
- **Tier B (suggestions, report-only)**: split-payment or tolerance matches —
  amount in `[50 %, 100 %)` of the charge → returned as `suggestions` for manual
  confirmation, never auto-linked.

### Enrich & persist (apply step)

- Parsed purchases are stored in a new `amazon_purchases` table (unique per
  workspace + order + tracking + ship-date) so re-imports are idempotent and the
  data stays queryable (agents/MCP can be taught to read it later).
- A linked purchase appends `Amazon #<order-id>: <item names>` to the charge's
  `notes` (via `merge_notes`, so re-imports don't duplicate text). Rules can then
  classify on `notes contains …`, and agent context sees item names.

## Deliberately out of scope (follow-ups)

- Importing unmatched purchases as *new* transactions (double-count risk until
  pending-charge placeholders exist).
- Auto-applying Tier B suggestions; rule re-evaluation after enrichment; MCP tool
  `list_amazon_purchases`; frontend import UI (this PR is spec + backend + tests).
- Non-US exports and digital-goods rows (no tracking, no card) — dropped by the
  same last-4 rule.

## Privacy note

The raw export contains name, home address, and full order history. Test fixtures
built from a real export must be scrubbed (names/addresses replaced) — the fixture
in `backend/tests/fixtures/` is.
