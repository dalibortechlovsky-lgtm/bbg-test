# Front Office Trade Control

Initial control-layer prototype for the workflow:

1. UBS KeyTrader execution
2. Bloomberg chat or KeyTrader EOD ingestion
3. Canonical trade ledger
4. Bloomberg MARS/PTT booking adapter
5. UBS positions reconciliation
6. Exceptions-only weekly control

## Current scope
- Canonical trade schema
- Bloomberg-style chat parser
- Source/status and deduplication logic
- KeyTrader order-enquiry CSV importer
- UBS positions and cash snapshot importer with persistent storage
- Position reconciliation scaffold

External Bloomberg/UBS connectivity is intentionally behind adapters until real schemas/API access are available.

## Development dashboard

Requires Python 3.12; the dashboard uses only the standard library.
From the existing checkout (no additional worktree needed):

```sh
python -m src.server --port 8000
python -m unittest discover -s tests -p 'test_server.py' -v
```

The dashboard binds to loopback and displays the latest imported UBS holdings and
cash snapshot. It supports positions and KeyTrader order CSV upload, group filtering, a persistent
trade blotter, and an import review history. It does
not calculate live multi-asset P&L or risk, submit orders, or expose a remotely
accessible service. The existing ledger remains a prototype.

## Windows Bloomberg probe

On the Windows computer with an authenticated Bloomberg Terminal, install the
Python SDK following Bloomberg's official Desktop API installation instructions.
Then run from this checkout:

```powershell
python bridge/bloomberg_quotes.py "IBM US Equity" "EURUSD Curncy"
```

This makes a read-only reference-data request to localhost:8194. Security and
field errors are retained in the JSON response; a response is not proof every
requested field is entitled. Live operation has to be validated on Windows.
The probe is the first bridge component, not a persistent cloud connection.
Do not expose Desktop API port 8194 to the Internet.

## Integration requirements

UBS KeyTrader API/FIX availability must be confirmed for this account. Until then,
the supplied order-enquiry export format is supported by the local importer.
Individual fills, broker corrections, and other export formats remain unsupported.
For options, futures, and bonds, valuation also requires contract multipliers,
expiry/strike, quote conventions, accrued interest, and currency conversion.
Remote phone access and ChatGPT tools still require a hosted authenticated service,
a secured outbound Windows bridge, and Bloomberg-approved data usage. None of
those connections are active in this prototype.


## KeyTrader order-enquiry import

```sh
python -m src.keytrader /path/to/enquiry_orders.csv
python -m src.keytrader /path/to/enquiry_orders.csv --persist
python -m unittest discover -s tests -p 'test_*.py' -v
```

Supports `sep=;` and literal Excel text wrappers `=("...")` without evaluating
spreadsheet expressions. Imports only Completed orders with zero open quantity,
positive average price, and Units quantity type. Uses ISIN as security identity,
custody account, order number, currency, instrument description and trading place.
`Entered on` is preserved as order-entry time with unspecified timezone, never
execution time. Limit price is not used as the execution price.

Records are labelled order_execution_summary: aggregate order quantities and
average prices are not individual fills. Other states are returned for review;
partial fills cannot be reconstructed from this export. Invalid batches accept
no trades. The ledger deduplicates by source, account and order number. Order
summaries are not automatically matched against Bloomberg chat instructions.
Without `--persist`, the CLI validates only. With `--persist`, it saves summaries
and import review in the same database used by the dashboard. Neither mode books
or submits trades. Exports do not establish current holdings,
settlement, fees, or complete historical positions. Bond nominal quantities and
derivative conventions need representative exports before support is added.


## UBS positions and balances

```sh
python -m src.positions "/path/to/positions.csv"
python -m src.server --port 8000
```

Alternatively, select the positions CSV in the running local dashboard and choose
Import snapshot. The API accepts UTF-8 CSV; the dashboard and CLI also decode
Windows-1252 exports. No external dependencies or live UBS access are needed.

The importer supports the supplied sectioned export: each repeated header defines
its own columns, and the footer supplies valuation currency, valuation date,
export timestamp and expected position count. It validates the whole file before
saving. Cash accounts, call deposits and securities remain separate. Options with
no ISIN retain their Valor identity; deposits sharing a product retain distinct
serial identities. Raw source fields, bond percentage quotes, broker-reported
unrealized P/L percentages, and dated exchange rates are preserved. Group names
come from UBS and are not independently verified asset classifications.

Amounts use decimal strings. Market values, cost values and accrued interest are
reported in the footer valuation currency; account balances are native currency.
Intraday balances are separate and are not added to account balances. No naive
quantity-times-price valuation or historical FX conversion is applied. Accrued
interest is displayed separately from the sum of reported market-value fields;
no independently verified total NAV or daily/realized P&L is claimed. The export
is not evidence of available buying power. Derivative terms and multipliers still
need verification before live valuation or Greeks can be computed.

Validated exports are retained atomically in `data/frontoffice.sqlite3`, excluded
from Git. Keep a protected backup if this local data needs to survive loss of the
workspace. Identical content is idempotent; latest display is selected by valuation
date, then export timestamp. Uploading an older snapshot does not replace newer
holdings. The current dashboard assumes a single portfolio; multi-portfolio
selection is not implemented. Export timestamps have unspecified timezone.

Use `--db /path/to/database.sqlite3` with both the positions CLI and server to
select another database. Tests use temporary databases and synthetic accounts:

```sh
python -m unittest discover -s tests -p 'test_*.py' -v
```

The endpoint `/api/status` includes dated holdings, cash and summary values, but
omits raw source fields such as IBANs. `POST /api/import/positions` requires
`Content-Type: text/csv` and `X-Import-Format: ubs-positions`, accepts at most 2 MiB,
and does not allow cross-origin dashboard uploads. This remains a local development
service, not an authenticated remote-access deployment.


## Persistent order history and review

The dashboard supports KeyTrader orders via its Import orders form and displays
accepted completed order summaries sorted by order-entry timestamp. Individual
fill times, settlement dates, fees and realized P&L remain unavailable. Imported
orders are not automatically applied to an existing UBS holdings snapshot: the
orders may already be reflected there, and the history is incomplete.

Storage keys include source, custody account and order number. Identical files are
idempotent; overlapping exports do not duplicate equivalent orders. Decimal-scale
changes alone are equivalent. Conflicting stored orders, or non-completed states
for a previously completed order, reject the entire new batch without overwriting
history. Parsed rejected batches, cancelled/working rows, and duplicate reviews are
retained with UTC import timestamps. Malformed headers/CSV are rejected before an
import record can be created. The dashboard shows the latest 20 import reports;
all completed import reports remain in the database. Corrections require a separate
review workflow; no automatic overwrite or override is provided.

`POST /api/import/orders` requires `Content-Type: text/csv` and
`X-Import-Format: ubs-orders` with the same 2 MiB and origin checks as positions.
HTTP 409 returns a rejected batch report; HTTP 400 indicates a malformed input.
`/api/status` includes the blotter and import reviews. Use `--db` with the order
CLI to select a database, and use that same database with the server.

## Standalone Windows Bloomberg probe

The `bridge/` folder can be copied to the Windows Terminal computer independently
of the dashboard. Follow `bridge/README.md`; `run_probe.cmd` provides a sample
read-only Desktop API request after the official SDK has been installed. The
package does not establish a persistent bridge or a ChatGPT connection. Windows
operation, SDK installation and real Terminal entitlements remain unverified here.

## Persistent Bloomberg polling worker

`bridge/bloomberg_bridge.py` is a standalone Windows-side process that polls
Desktop API reference data and writes a local JSON state file. See bridge/README.md
for startup, timestamp semantics and outage behavior. Offline tests exercise
reconnection state, missing/unentitled data and atomic output. They do not validate
continuous Windows execution or live data. A stable authenticated HTTPS deployment
and outbound transport remain required to connect it to this dashboard or ChatGPT.


## Hosting preparation

`Dockerfile` and `deploy/compose.yaml` prepare an application container on a private
network behind password-protected Caddy HTTPS. Image versions are pinned by digest;
SQLite and certificate state persist in volumes. `deploy/README.md` documents the
proposed Hetzner server, account prerequisites, domain, startup and backup procedure.
`src.backup` creates consistent database backups without overwriting existing files.
A Hetzner CPX22 server was created on 13 October 2026 using the securely configured
HETZNER_API_TOKEN. See deploy/DEPLOYMENT.md for state and pending public checks.
No custom domain has been registered. Dashboard passwords belong in the
server's private configuration, never Git/chat. Basic authentication is the initial
single-user sign-in; multi-user/MFA and bridge publishing remain unimplemented.


## Authenticated Bloomberg integration (prepared release)

Scoped 90-day device tokens, revocation, HTTPS quote ingestion, portfolio ISIN/FX
watchlists, quote timestamps and stale-state display are implemented. Prices live
in separate tables from UBS snapshot valuations. The dashboard can distribute the
standalone Windows worker and private device configuration after deployment.

The existing live server still needs this source update. deploy/update.sh preserves
its .env, passwords and Docker volumes, backs up its database/source, updates only
application code, and verifies record counts, snapshot identity and device scopes
without inserting synthetic prices. Failures restore the old source while retaining
the database. Run only with the reviewed immutable release commit. SSH is closed
and this workspace has no TCP grants, so installation requires the server owner's
Hetzner root Console. Do not rebuild the VM or recreate its database.

Offline and local HTTPS tests validate this integration; a live Windows-to-hosted
upload has not yet occurred. Generic ISIN listing resolution and missing-ISIN options
still need mapping before live portfolio valuation. Bloomberg chat, UBS execution,
MARS booking and ChatGPT tool integration are not active. The risk module below provides Greeks for supported instruments with explicitly verified inputs.

## Portfolio analytics

`src.analytics.portfolio_analytics` is a read-only Decimal calculation included in
`/api/status` and the dashboard. It reconciles broker market value, reports native
cash/deposits and net/gross denomination allocation, and computes snapshot
unrealized P/L only for securities with UBS cost values (assumed in the export
valuation currency). Cost coverage is explicit; accrued interest is separate.

The mixed-date indicative value applies same-currency, positive, recent Bloomberg
PX_LAST / UBS market-price ratios only to the UBS equity-investment group with
amount quotes. This assumes unchanged listing, quote units, holdings and no
corporate actions; it is not a verified valuation or daily P/L. Recent positive
CCYBASE Curncy quotes in base currency adjust broker values using the direct
export FX rate, or its inverse. Missing/stale/incompatible prices and unsupported
instruments retain UBS prices; FX may still be adjusted. Latest fresh device
quotes are selected deterministically by observation time. No bank snapshot is
modified and no order summaries are applied to holdings.

Bonds, structured products and unmapped options require verified terms before
price repricing. Daily/realized P/L, Greeks, underlying currency exposure and
portfolio risk remain unavailable until fills, cash flows, opening valuations
and instrument terms are supplied. Currency allocation is denomination, not delta.

Run `python -m unittest discover -s tests -v`. The source update installer checks
analytics reconciliation and preserves the existing records/credentials.

## Average-cost P/L, derivatives risk and trader report

The dashboard now has a separate performance ledger (`src/performance.py`),
verified-input risk store/model (`src/risk.py`), and trader report
(`src/report.py`). No external Python dependencies are required. These are
available through authenticated Caddy routes; bridge bearer tokens cannot read
reports, holdings or event ledgers.

### Performance events

Download `/api/templates/performance.csv` through the dashboard. UTF-8,
comma-separated columns in this exact order:

```
event_id,date,sequence,type,account,security,currency,quantity,price,multiplier,fees,amount,description
```

Dates are actual trade/valuation dates, YYYY-MM-DD; same-day inventory chronology
is explicit with nonnegative unique `sequence`. `event_id` is globally unique.
Rows are append-only and idempotent. Conflicts or ambiguous ordering reject the
whole batch. Corrections/deletions are not yet supported; verify the source
records before importing. A new security requires an explicit zero OPENING row;
existing longs/shorts require an opening signed quantity and native average cost
price including historical fees. OPENING fees are zero. One baseline is allowed
per account/security/currency. Earlier historical fills need a correspondingly
earlier baseline; do not import a later baseline as a reset.

BUY/SELL quantity is positive, price is per quoted unit and multiplier must be
verified (1 for ordinary shares, contract multiplier for options, 0.01 only for
confirmed percent-of-nominal bond units). Fees are nonnegative in trade currency.
The engine calculates signed average cost, including opening trade fees,
proportional closing fees and allocation of fees when a trade crosses zero.
Missing baselines or changing multipliers block that instrument's P/L; no
inferred inventory or corporate actions. Results are native-currency accounting
figures, not tax reports or historical FX-inclusive base-currency gains.

INCOME records a net amount. CASHFLOW records an external transfer (deposit
positive, withdrawal negative), not a trade/dividend/interest movement.
VALUATION records complete closing account NAV including cash, in its stated
currency. For these types, inventory fields and security are blank. Daily P/L
is next closing NAV minus prior closing NAV minus external flows after the prior
close through the next close. Non-consecutive dates are explicitly period P/L;
account/currency scopes remain separate. No automatic completeness or brokerage
reconciliation is possible from these manually supplied totals. YTD totals are
imported realized and income only, not total YTD portfolio P/L or return.

KeyTrader order summaries remain separate: execution dates, fees and actual
fills cannot be inferred from order-entry timestamps. The TR PDF is a layout
reference; its historical totals have not been imported as ledger evidence.

### Derivatives risk

Download `/api/templates/risk.csv`. Required columns:

```
position_id,kind,underlying,currency,multiplier,strike,expiry,spot,volatility,rate,dividend_yield,observed_at,verified
```

The dashboard lists holding IDs. `verified=yes` is a user assertion that terms
and conventions have been checked. Supported kinds: european_call,
european_put, future. Currency must equal holding currency; quantity is signed
and comes from the dated UBS snapshot. Supply timezone-aware ISO expiry and
observation timestamps. Options use decimal volatility/rates/dividend yield
(e.g. volatility 0.25), ACT/365 and continuous yield. Futures spot is the futures
quote, with linear risk; price times notional is deliberately not shown as
asset MTM. Older observations cannot replace newer stored ones. Imports are
atomic. Inputs older than 15 minutes retain dated individual modeled figures
but are excluded from aggregates; expired instruments require settlement data.

Black–Scholes Greeks: delta units/cash, signed gamma's quadratic P/L for a 1%
spot move, vega per one volatility percentage point, theta per calendar day.
Full repricing scenarios: spot ±5%/±10%, volatility +5 points. Currency aggregates
assume simultaneous relative shocks across all modeled underlyings. No
American/exotic/structured model, implied-vol solver, VaR, margin, assignment,
cash-flow/corporate-action or bond-DV01 engine. PX_LAST bridge uploads do not
refresh these separate risk inputs. Verified terms/market data are prerequisites.

### Trader report exports

`/reports/trader` is a printable view. `/api/reports/trader.html` downloads a
standalone HTML report; `/api/reports/trader.csv` downloads UTF-8 BOM CSV.
Report controls default size currency EUR and snapshot unrealized currency CZK,
matching the reference's column convention; both can be changed. Conversions
use direct/inverse/cross dated export FX, with missing rates shown unavailable.
Report sections: equities, bonds, derivatives/ETFs/structured products, cash and
deposits/FX; average-cost realized history; income; daily/period P/L; derivatives
risk. Native realized currency totals are separate. Snapshot unrealized P/L is
not relabeled YTD. No copied logos or historical totals. Negative/positive P/L
shading follows the reference; exposure values are not treated as gains/losses.
Use Print / Save as PDF (portrait A4, print backgrounds enabled). Text is HTML
escaped and CSV formula-like strings are neutralized.

Validation: `python -m unittest discover -s tests -v`; frontend Node syntax check;
local Chromium render/download/print checks; `deploy/smoke_test.py` against a
locally built `bbg-frontoffice:deployment-test` validates authenticated HTTPS
report downloads, scoped device access, persistence and backup restoration.
The updater validates new features and retains existing performance/risk row
counts without inserting synthetic records into a live account.
