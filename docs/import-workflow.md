# Proposed owner import workflow — rollout paused

The owner requested a simpler workflow before rollout. Native UBS account activity
and a combined positions/activity upload are implemented in development. Whole
portfolio P/L, automatic folder upload and the PTD accounting integration remain
incomplete; this version has not been deployed.

## Sources and scope

Owner-confirmed scope: the complete UBS portfolio, including cash/deposits,
securities and derivatives. Keep bank account/portfolio scopes separate. A
trading-book-only scope requires an explicit definition of allocated cash and
transfers; it must not borrow unrelated deposit values for P/L denominators.
Report currency is independently selected; broker and MARS display currencies
are not automatically the accounting currency.

UBS is authoritative for executions, fees, cash, settlements, opening quantities
and historical costs. Bloomberg provides independent market prices, historical
FX and instrument metadata where entitled/verified. MARS is an optional one-off
identifier/terms reference; its calculated currency values, P/L and Greeks remain
excluded. PTD is a dated-trade fallback and audit source, not evidence of cash
funding or complete opening costs. Do not add PTD and UBS copies of the same
execution to a single ledger.

## Initial pack (one-time)

1. The prior year-end UBS positions/statement, including carried quantities,
   native historical average costs, cash/deposits and the year-start valuation.
   Historical average cost and year-start market value are different inputs.
2. UBS account activity from the start of the reporting year through a declared
   export end date: dated buys/sells, actual quantities/prices, commissions/fees,
   dividends/coupons/interest, external deposits/withdrawals, FX conversions,
   exercises/expiries/redemptions and futures margin/settlements when applicable.
3. A current UBS positions/balance export at a clearly identified valuation date.

Native CSV/Excel exports are preferred. A statement PDF can be used once for
reviewed extraction of missing opening basis or private-product terms. Owners
should not be required to rewrite data into the internal performance CSV.
Existing uploaded files can be reused; request only missing coverage.

## Routine pack

One import area accepts current positions and all account activity files together,
identifies their formats, validates the entire batch and avoids duplicates. The
supplied UBS format exports one file per currency account. The normal routine is
therefore one positions file plus EUR, USD and CZK activity files, selected in one
upload. Any other active UBS account needs its own activity coverage too.

These files include dated trades, prices/quantities, net settlements, income,
deposit principal, FX conversions and futures variation margin. They avoid a
routine PTD upload for the covered postings. Instrument terms, fee/tax breakdowns,
corporate actions and in-kind transfers can still require a source confirmation.
PTD remains an independent cross-check; the owner confirmed that a January entry
was a year-start reset of an existing holding, not an additional execution. An
opening market mark must never replace the UBS historical acquisition cost.
The KeyTrader enquiry report has order-entry timestamps and aggregate completed
orders, not dated fills. MARS is not a routine requirement. Overlapping account
date ranges are safe to re-import; changed economic transaction components are
rejected for correction review rather than silently added.

A future optional watched Windows folder can make saving native exports the only
routine owner action. This needs an explicitly configured folder and a separate,
scoped upload-only credential, HTTPS, idempotency, audit and failure indicators;
the quote-only device token must not silently gain account-data permissions.

## Automatic work and exceptions

The system reconstructs inventory, average cost and trade cash in native units;
classifies actual external transfers separately; obtains independent price/FX
history; applies verified contract terms; and checks quantities, costs and cash
against the dated UBS snapshot. Broker corrections preserve superseded records.
Known owner-confirmed corrections are persisted; new ambiguous versions are
presented for a short review and never silently collapsed.

Financial forms should be an exception path. First obtain prices, currency,
ISIN/BBID links and contract metadata from Bloomberg/broker data. Unsupported
OTC contracts or absent opening costs require their original confirmation or
statement, rather than recurring manual entry of model inputs. Do not infer
multipliers from untrusted converted totals. Blank fees and empty cash reports
are not evidence of zero fees/transfers or complete coverage.

## What the owner sees

- One import action and a concise result: coverage period, imported records,
  corrections, matched holdings/cash and remaining exceptions.
- Clear valuation date and transaction coverage end date. The latest transaction
  date alone is not proof that an export covers subsequent no-activity days.
- P/L status: recorded-trade subset, provisional estimate, or reconciled account
  P/L. Intraday prices are not finalized closing prices.
- The same status and coverage in report exports; no unsupported total daily/YTD
  number and no zero substituted for missing data.

Final daily/YTD account P/L needs complete closing values and external-flow
coverage. Trades/futures notionals are not external deposits or cash payments.
Accrued interest conventions must be consistent between opening/closing NAV.
Native realized average-cost P/L and full report-currency P/L including historical
FX are distinct outputs. Do not call the sum of lifetime unrealized P/L and YTD
realized P/L total YTD portfolio performance.

## Evidence checked and remaining owner inputs

The three supplied files contain 408 cash postings. Every export reconciles both
opening plus movements to closing cash and each running balance in source order.
Shared references on fees and payment principal remain separate components;
positive debits and negative credits on reversals are retained with their signs.
Nine cross-currency FX transactions each have two matching legs. Futures margin
is recorded as cash settlement, not a purchase of contract notional. Deposit
principal is an internal asset movement, not external funding.

The USD file's trade-date filter ends on 8 October 2026; EUR and CZK end on
30 September. A USD posting is booked after its filter end, which explains the
booked versus intraday snapshot cash difference. A filter end cannot be used as
a certified booking-date cash close. Matching balances on different dates cannot
establish coverage for the intervening days.

Still needed from the owner: the 31 December 2025 positions/statement with carried
quantities, native acquisition costs and year-start market values, plus account
exports and a positions snapshot through the same current cutoff. Use a single
declared cutoff for all accounts. Resolve only exceptional private contracts,
corporate actions or missing acquisition history from confirmations. The existing
zero CHF snapshot does not establish that there was no CHF activity.

The platform still needs independent historical closing prices/FX and a consistent
accrual/settlement valuation convention. A live PX_LAST quote is not a historical
close. These are system implementation requirements rather than recurring owner
worksheets. Main report currency is CZK; native asset returns remain visible.

## Owner-confirmed attribution design

The main report includes the whole UBS portfolio, including cash and deposits,
with CZK as report currency. Asset contribution measures security price/contract
performance plus dividends, coupons and deposit interest, net of attributable
fees. Native asset return is shown separately from FX. Currency contribution is
shown by currency under Derivatives, ETFs and FX and covers foreign holdings,
cash and deposits together. CZK has no translation contribution to itself.
Actual FX hedges/conversion costs require a clearly defined FX attribution rule;
hedge payoffs must not also appear in ordinary derivative asset contribution.

Use one explicit start-of-period FX convention: translate local asset P/L at
opening FX; put the price/FX interaction in the currency bucket using ending
native exposure. For a currency bucket:

- B = opening native portfolio value, including cash and relevant liabilities.
- E = closing native portfolio value.
- C_i = dated native capital movements across this currency bucket boundary.
- X0/X1 = opening/closing CZK per one native currency unit.
- X_i = the independently verified translation rate at each capital movement.
- Native asset P/L = E - B - sum(C_i).
- Asset contribution CZK = native asset P/L * X0.
- FX contribution CZK = E * (X1-X0) + sum(C_i * (X0-X_i)).

Their sum is E*X1 - B*X0 - sum(C_i*X_i). Same-currency security/cash trades cancel
inside a bucket. External funding is excluded from performance; internal
cross-currency conversion legs require matching and treatment of execution costs.
Transfer between UBS subaccounts inside the selected portfolio is not an
external portfolio deposit. Cross-currency flow translation must reconcile and
must not use the untrusted MARS/legacy PTD conversion results.

Example without flows: USD asset 100 -> 110; USD/CZK 25 -> 24. Native asset return
is 10%; asset contribution +250 CZK; currency contribution -110 CZK; total
+140 CZK. FX is counted once, including the interaction under this convention.

For YTD, the opening market values/FX are prior year-end values for carried
positions; positions opened later use their actual entry/capital movements.
Original acquisition average cost remains necessary for accounting realized P/L,
which is separate from YTD asset attribution. A carried asset with lifetime cost
50, year-start market price 80 and sale price 90 has lifetime realized gain 40
but YTD price contribution 10, before currency/fees. These must not be combined
or mislabeled. Daily contributions can be accumulated for the annual report
using a consistent valuation cutoff and known cash-flow coverage.

Closing/accounting values must include pending trade receivables/payables when
trade-date positions and settlement-date cash differ, as well as accrued interest
under a consistent convention. Futures notionals are not purchased assets or
cash flows. Variation-margin/settlement records and verified option multipliers
are required for their contributions. Missing coverage remains provisional.

The routine owner workflow stays native exports, not attribution worksheets.
Bloomberg supplies verified price/FX histories where available; missing private
contract or opening-basis inputs are resolved once from their source documents.
