# Amazon Order History import & purchase→transaction matching

Status: draft (PR candidate) · Scope: backend + import UI draft

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

- 2,132 rows → 1,398 distinct `Order ID`s → ~1,579 (order, tracking) groups →
  **1,536 charge candidates** after the parser drops zero-amount and no-card
  rows.
- A single order split into 4 shipments appears as 4+ rows with **different**
  `Carrier Name & Tracking Number` (TBA…) values — each was a separate card
  charge. And 89 tracking strings are reused across *different* orders, so
  the grouping key must carry the order id too.
- `Total Amount` is **per row (item)**, including item tax and any shipping charged.
  The card charge for a shipment is the sum of `Total Amount` over the rows sharing
  one tracking number. (`Shipment Item Subtotal` is repeated per shipment and would
  double-count if summed.)
- `Payment Method Type` carries brand + card last-4: `Visa - 9371`, and split
  payments like `Gift Certificate/Card and Visa - 7944` (gift card covered part of
  the cost, so **the card charge is lower than the row total** — ~2% of rows here).
- `Ship Date`/`Order Date` arrive as ISO timestamps, sometimes with milliseconds
  (`2026-05-05T10:54:00.704Z`), sometimes as `Not Available`. Amazon charges when
  a shipment leaves the warehouse, so charge date ≈ ship date.
- A few rows carry **two** tracking numbers ("Shipped and Shipped"): the item
  shipped in two parcels, so its `Total Amount` may not equal any single card
  charge — flagged report-only like split payments.
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
  excluded, mirroring recurring-bill matching). Split payments and compound-
  tracking charges skip this tier entirely: their export total is not a
  trustworthy charge amount, and an exact-amount debit inside the window is
  more plausibly some other purchase's payment.
- **Ordering**: charges are matched in ship-date order, not file order, and
  each tier picks the earliest unused posting date. Real exports repeat
  amounts heavily (769 of 1,536 charges share an amount with another charge;
  $10.81 appears 27 times), so file-order matching could pair a charge with
  the later twin's payment.

- **Tier B (suggestions, report-only)**: anything else in `[50 %, 100 %)` of
  the charge amount inside the window — split payments, compound-tracking
  rows, tolerance near-misses → returned as `suggestions` for manual
  confirmation, never auto-linked.

## Calibration (against a real 2,132-row export)

`backend/scripts/calibrate_amazon_match.py <export.csv>` rebuilds a "perfect
statement" from the export (one synthetic debit per charge, posted ship+1;
split payments seeded 20 % low) and runs the real matcher against it. On the
2,132-row export: **1,536 charges parsed → 1,478 auto-linked (every non-split
charge), 0 mispaired links, 58 report-only suggestions**, re-import fully
skipped, whole match under 0.15 s. The 28 same-amount pairs whose windows
overlap are paired correctly via ship-date order + earliest-posting pick.

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
  `list_amazon_purchases`.
- Import UI: a third "Purchases" tab on the import page (`amazon-import-panel.tsx`)
  is drafted in this PR — drop zone, dry-run preview with per-pair counts, card
  selector, and apply. It has no component tests yet and reuses the statement
  importer's look deliberately.
- Non-US exports and digital-goods rows (no tracking, no card) — dropped by the
  same last-4 rule.

## Privacy note

The raw export contains name, home address, and full order history. Test fixtures
built from a real export must be scrubbed (names/addresses replaced) — the fixture
in `backend/tests/fixtures/` is.
