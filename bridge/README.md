# Bloomberg Terminal connection check (Windows)

The one-shot probe checks read-only Desktop API access. It does not place
orders, expose a network service, or send data remotely. The optional configured
worker uploads quote snapshots to your authenticated dashboard, as described below.

1. Use the Windows computer running Bloomberg Terminal and sign in to Terminal.
2. Install a Python version supported by Bloomberg's current Python SDK. Use
   Bloomberg's official API Library instructions to install its Desktop API SDK
   and Python `blpapi` package, preserving TLS and package verification:
   https://www.bloomberg.com/professional/support/api-library/
3. Extract this package into a local folder. Open Command Prompt in that folder.
4. Check Python and the Bloomberg package:

   ```cmd
   python --version
   python -c "import blpapi; print(blpapi.version())"
   ```

   If your Python installation uses the Windows launcher, use `py -3` in place
   of `python` in these commands and the next one.
5. Run:

   ```cmd
   python bloomberg_quotes.py "IBM US Equity" "EURUSD Curncy"
   ```

   Alternatively, double-click `run_probe.cmd` to run those sample requests.
6. Expect JSON security results with `fieldData` containing `PX_LAST` and `CRNCY`.
   Check for `securityError` and `fieldExceptions`: a JSON response alone is not
   proof that the securities or fields are entitled. If errors occur, share the
   error text, excluding account information or credentials. If successful, say
   that both sample securities returned prices; no need to share licensed prices.

The probe connects only to localhost port 8194. A session failure requires checking
Terminal login and Desktop API availability with Bloomberg support. Do not open
port 8194 to the Internet. Access to reference data does not establish MARS/PTT,
chat history, trading permissions or UBS connectivity. The next bridge phase needs
a secured outbound connection and agreed use of the licensed data.

## Continuous local worker

After the one-shot probe succeeds, copy `bloomberg_bridge.py` to the Windows
Terminal computer and run:

```cmd
python bloomberg_bridge.py "IBM US Equity" --interval 15
```

The default output is `data/bloomberg.json` beside the script. Use `--output` to
choose a different local path. The worker polls reference data, opens a new session
per cycle, and reconnects automatically. Failed cycles back off to at most 60
seconds. It is not a streaming subscription. Stop with Ctrl+C; the status changes
to stopped while previous quote timestamps are preserved. For continuous use,
Windows Task Scheduler can run this command under the same user and interactive
Terminal session; configure restart on failure. This scheduling has not been
performed or verified from the cloud environment.

`received_at` is the UTC time our bridge received a successful complete response,
not the exchange's trade timestamp. `checked_at` records the poll attempt.
Missing securities, field errors and missing PX_LAST values produce degraded
status without replacing the last good quote set. Local file replacement is atomic.
Without --config, quotes stay in the local file. Configured HTTPS upload is
implemented in the prepared integration release below. No ChatGPT tool is active.

Local download links have not worked in the user's chat interface. Do not assume
those links transfer the package to Windows. Use a reviewed copy/paste script or
an explicitly authorized repository/file distribution method for the next test.


## Connect to the hosted dashboard

After the server update is installed, sign into the dashboard and open Connect your
Windows Bloomberg Terminal. Download the worker, then create a device and download
bbg-bridge-config.json. Store the two files in the same local folder; the config is
private and must never be shared or committed to Git. Run on the Windows Terminal:

```powershell
python bloomberg_bridge.py --config bbg-bridge-config.json --once
python bloomberg_bridge.py --config bbg-bridge-config.json
```

The first command performs one poll/upload for validation. The second runs until
Ctrl+C, polling every 15 seconds with reconnection/backoff. No extra Python library
is required beyond the already working Bloomberg SDK. The worker uses standard
HTTPS proxy/CA settings and refuses redirects so credentials cannot move hosts.
Do not disable TLS verification. Upload/watchlist errors report HTTP status without
printing tokens, account data or price values. Watchlists refresh every minute.

The watchlist derives from imported portfolio ISINs plus currency/base-currency
pairs. Generic ISIN lookup may select another listing/currency. Options without
ISINs are reported as unmapped; do not guess their Bloomberg identifiers. PX_LAST,
CRNCY and ID_ISIN are requested. Optional field errors do not discard valid last
prices. Missing prices retain their old receipt timestamps while valid securities
continue updating. Uploaded quotes do not change UBS values, P&L, quantities or risk.

Device tokens expire after 90 days and can be revoked in the dashboard. The server
stores only token digests. The token is scoped to quote upload and security watchlist
access, not bank accounts, CSV import, token management or order submission. The
dashboard marks stale quotes or silent/disconnected devices after 90 seconds.
Receipt timestamps are not exchange trade timestamps; a fresh request can return
an unchanged or prior-session last price.


The current dashboard watchlist can also request historical daily CZK FX fixings.
The worker uses `HistoricalDataRequest`, `PX_LAST`, all calendar days and previous
values on non-trading days, then uploads only requested currency/date/price rows
to `/api/bridge/history`. Missing or conflicting fixings remain pending. Replace
the worker file and restart it with the existing private configuration to enable
this feature. Local SDK contract tests do not prove Windows entitlements.
