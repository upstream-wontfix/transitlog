# transitlog

Download your own transit-card history (every trip, every purchase, receipts)
and get a usage report out of it. Supports **Clipper** (SF Bay Area) and
**PRESTO** (Ontario / Metrolinx).

> [!WARNING]
> **Provided AS IS, with no warranty and no liability. Written with AI assistance.**
> Review the code before you run it, and only use it on your own account.
> Read [DISCLAIMER.md](DISCLAIMER.md) before use.

clippercard.com shows trip history in pages of ten and only offers downloads for
one card and one date range at a time, capped at 100 rows. This tool:

- logs in with your account
- walks the whole range month by month, splitting busy months until nothing is cut off
- de-duplicates the results
- gives you one clean CSV or JSON file

It is **read-only**. It can only call the login, history, download and receipt
endpoints. Checkout, card removal and fare disputes are not on its allowlist
and can't be reached.

## Who it's for

It exists to get your own transit data out of a slow, click-heavy website in a
clean, scriptable form. People use that for:

- **Tax, expense and benefit claims.** Transit is deductible or reimbursable in
  a lot of places (commuter benefits, work-from-anywhere stipends, medical-travel
  claims, small-business expenses). `report` gives you totals by operator and
  month, and on Clipper `receipts`/`statements` pull the official PDFs to attach.
- **Personal records and research.** Keep your own archive, or just answer
  questions the official app won't: how much did I spend on BART this year, which
  stations do I actually use, how have my habits changed, weekday vs weekend.
- **AI and agent workflows.** It's a plain CLI with JSON output (`--json`, or
  `export` to JSON), so you can pipe your travel history into an LLM or an agent
  ("summarise my commute costs for Q2", "flag months I overpaid vs a pass") and
  work on real data instead of screenshots.
- **Automation and home dashboards.** Because it's a scriptable command, it drops
  into tools like **n8n**, **Home Assistant**, cron jobs or shell scripts — e.g. a
  monthly job that exports last month's CSV to a folder, a dashboard card showing
  card balance and this month's spend, or an alert when a balance runs low.
- **Spreadsheets and BI.** CSV goes straight into Excel, Google Sheets, a
  database or a notebook for your own charts and pivots.
- **And more!**

Whatever the use, the rule is the same: **your own account, your own data** — see
[DISCLAIMER.md](DISCLAIMER.md).

## What you can get

| Command | What it gives you |
|---|---|
| `cards` | each card on the account: card number, status, rider class, cash value, passes, autoload |
| `trips` | every tap: time, operator, start/end station or stop, fare, what paid for it (pass, cash value or free transfer) |
| `orders` | every purchase: cash-value loads, passes, card fees, with amount and payment reference |
| `receipts` | Clipper's PDF receipt for each order (useful for commuter-benefit or expense claims) |
| `statements` | Clipper's official trip-history PDFs, one per month (or half-month when busy) |
| `report` | summary: trips by operator, month and weekday, top stations and journeys, spending, pass cost per ride |
| `export` | all of the above into one folder |

## Install

```bash
git clone https://github.com/upstream-wontfix/transitlog && cd transitlog
uv tool install .              # or: pipx install .
transitlog clipper set-password   # stores it in ~/.config/transitlog/clipper.password (mode 600)
```

Config file at `~/.config/transitlog/config.ini`, one `[section]` per provider:

```ini
[clipper]
username = you@example.com
; password_file = ~/.config/transitlog/clipper.password

[presto]
username = you@example.com
; password_file = ~/.config/transitlog/presto.password
```

The password is never accepted as a command-line flag. It comes from, in order:

1. `$TRANSITLOG_CLIPPER_PASSWORD`
2. `--password-file`, `$TRANSITLOG_CLIPPER_PASSWORD_FILE`, or `password_file` in the config file
3. `~/.config/transitlog/clipper.password`

The username can also be given with `--username` or `$TRANSITLOG_CLIPPER_USERNAME`.

## Usage

```bash
transitlog clipper cards
transitlog clipper trips                          # last 365 days, as a table
transitlog clipper trips --since 2026-05-01 -o trips.csv
transitlog clipper trips --days 30 --json
transitlog clipper orders -o orders.json          # .json → JSON, anything else → CSV
transitlog clipper receipts -d receipts/          # skips receipts already downloaded
transitlog clipper statements --since 2026-06-01 --until 2026-06-30
transitlog clipper report --since 2026-05-01
transitlog clipper export -d clipper-export --statements
```

PRESTO works the same way — swap `clipper` for `presto`:

```bash
transitlog presto set-password          # stores ~/.config/transitlog/presto.password (mode 600)
transitlog presto cards
transitlog presto trips --since 2026-01-01 -o presto-trips.csv
transitlog presto report --since 2026-01-01
transitlog presto export -d presto-export
```

Common options:

- `--since` / `--until` / `--days N` choose the range. The default is the last
  365 days, in Pacific time.
- `--card` picks one card by account number, card number or last four digits.
  The default is every card on the account.
- `--json` gives machine-readable output.
- `--debug` logs each request.

`export` writes `cards.json`, `trips.csv`/`.json`, `orders.csv`/`.json`,
`report.txt`/`.json` and `receipts/`, plus `statements/` with `--statements`.

### Sample report (made-up numbers)

```text
Transit usage 2026-05-01 to 2026-08-31

Card Commute card (0001): Active, cash value $12.50

Trips: 212 on 80 days (2.65 per active day)
First/last: 2026-05-04 07:52 / 2026-08-29 18:10
Paid with: 140 pass, 48 cash value, 24 free
Busiest hour: 08:00

By operator
  operator  trips    fares
  Muni        160    $9.00
  BART         44  $201.30
  Caltrain      8   $52.40
...
Passes
  pass                    bought     cost  rides  per ride
  Muni-Only Pass (Adult)       4  $344.00    140     $2.46
```

### Output fields

**Trips:** `trip_id`, `start_time`, `operator`, `start_location`, `end_time`,
`end_location`, `fare`, `product`, `tap`, `transfer`, `transfer_credit`,
`account_id`. Times are ISO 8601 with the provider's local offset.

- `end_*` is only filled in on tap-in/tap-out systems (Clipper: BART, Caltrain,
  ferries). PRESTO reports each tap separately, so `end_*` is blank there.
- `product` is a pass name, `Cash Value`, or empty for free rides such as
  transfers.
- `tap` is `on`/`off` where the source reports it per tap (PRESTO); blank on
  Clipper, which pairs the two ends into one row.
- `transfer` marks a ride taken as a transfer. PRESTO reports it exactly (a
  `TRANSFER` discount); Clipper has no transfer field, so it's inferred from a
  `$0` fare with no pass and should be read as a best-effort guess.
- `transfer_credit` is the transfer/co-fare credit amount when the source gives
  one (PRESTO); blank otherwise.
- JSON output also carries an `extra` object with anything else the source
  reported (e.g. PRESTO's `tap_mode`, `route_type`, `balance_after`, the full
  `discounts` list), so nothing is lost. CSV keeps only the columns above.

**Orders:** `order_id`, `time`, `amount`, `products`, `payment`,
`payment_ref`, `status`, `order_type`, `account_id` (plus `extra` in JSON).

The `report` adds, when the data supports it, a `Taps: N on, M off` line and a
`Transfers: N (transfer credit $X)` line.

## Things to know before running it

- **Only your own account.** It needs your clippercard.com login. Cards not
  registered to the account aren't visible.
- **It's slow on purpose.** It waits half a second between requests, and each
  month takes two (one filter, one download). A year of history takes about a
  minute; add a few seconds per receipt.
- **Wrong passwords.** A failed login is reported once and never retried, so a
  typo can't lock the account.
- **Bank-card (contactless) trips aren't included.** Clipper only shows those
  after you type the full bank card number, expiry date and security code into
  its website, and this tool won't handle those.

## Known quirks of Clipper's site

The tool works around all of these. They're listed so you can sanity-check the
output.

- **100-row cap.** Downloads stop at 100 rows without saying so. Busy months are
  split into halves (and further) until each piece has fewer than 100 rows.
- **One-year limit.** Ranges over about a year return "no trips". The tool
  requests one month at a time, so `--since` can go back as far as Clipper keeps
  data.
- **Stale downloads.** If a trip filter matches nothing, the website keeps the
  previous filter and a download returns that older data. The tool checks the
  filter result before every download and checks every row's date afterwards.
  If anything looks off, it stops instead of saving wrong data.
- **Orders page shows only the last 90 days.** Older orders still download
  correctly.
- **"All Cards" is broken.** Choosing it on the website causes a server error,
  so cards are fetched one at a time.
- **Window edges aren't in Pacific time.** A late-evening trip can show up in
  the next day's window. Each window is padded by a day and the results are
  trimmed locally.
- **Some receipts can't be downloaded.** Clipper's server fails on some older
  orders. Those are reported as warnings and the rest still download.
- **Stop names.** Muni buses often record no stop (left blank in the output),
  and some bus operators report numeric stop or route codes instead of names.

## PRESTO specifics

PRESTO (prestocard.ca, Metrolinx) is a single-page app, so the client signs in
differently from Clipper even though the commands are the same:

- **Sign-in is Azure AD B2C** (OAuth2 authorization-code flow with PKCE). The
  tool reproduces the browser's steps: load the sign-in page, read its hidden
  CSRF/transaction token, POST the credentials, follow the redirect and exchange
  the code for a token. As with Clipper, a failed login is reported once and
  **never retried**, so a typo can't lock the account. Accounts that require MFA
  or a captcha at sign-in can't be handled headlessly; the tool says so clearly.
- **Account data comes from Metrolinx's CIAM API.** It needs an Azure API
  subscription key, which is a public value the site ships in its own front-end
  config (not a user secret). A current value is built in; if Metrolinx rotates
  it, set `$TRANSITLOG_PRESTO_SUBSCRIPTION_KEY` to the `Ocp-Apim-Subscription-Key`
  your browser sends. It is **read-only**: only the account, media and
  transaction-history endpoints are on the allowlist; reload, purchase and
  card-management endpoints can't be reached.
- **Every tap is its own row, with its direction.** PRESTO records a tap-on and
  a tap-off as separate events, each with its own fare, so `trips` lists taps
  (`tap` = `on`/`off`) rather than paired journeys; `end_location` is always blank
  and the report's "top journeys" section stays empty. Fares are summed from each
  tap's actual charge. Transfers are detected exactly from PRESTO's `TRANSFER`
  discount, with the credit amount in `transfer_credit`.
- **`orders` covers loads and purchases only.** Internal system events (e.g. card
  migrations) are filtered out. `receipts` and `statements` don't exist for
  PRESTO — the tool reports that instead of inventing data.
- **All cards on the account are listed**, including hotlisted (lost/blocked)
  ones; use `--card` with a card number or its last four digits to pick one.

## Adding another card or network

The provider-specific code is isolated, so a new source plugs into the same CLI,
report and exports:

1. Write a client class with the same small interface the others use
   (`login`/`logout` as a context manager, `cards()`, `trips(since, until, card)`,
   `orders(...)`, `select_cards(...)`; `receipt`/`statements` optional). Clipper
   (a form login scraping CSV) and PRESTO (an OAuth/JSON API) show two very
   different backends behind the same shape.
2. Map its data onto the shared `Card`/`Trip`/`Order` records. Fill the common
   columns, and put anything source-specific in the record's `extra` dict so it
   survives into JSON exports without changing the schema.
3. Register it in `PROVIDERS` in `cli.py` (client class, timezone, whether it has
   PDF receipts/statements, help text). It then gets `cards`, `trips`, `orders`,
   `report` and `export` for free, and `transitlog <name> …` just works.

Credentials already resolve per-provider from `[<name>]` in the config file,
`$TRANSITLOG_<NAME>_USERNAME/PASSWORD`, or the matching flags.

## Python API

```python
from datetime import date
from transitlog import ClipperClient, resolve_credentials, build_report, render_report

with ClipperClient(resolve_credentials("clipper")) as c:   # same credential lookup as the CLI
    trips = c.trips(date(2026, 5, 1), date(2026, 8, 31))
    orders = c.orders(date(2026, 5, 1), date(2026, 8, 31))
    print(render_report(build_report(trips, orders, date(2026, 5, 1), date(2026, 8, 31), c.cards())))
    pdf = c.receipt(orders[0].order_id)
```

`PrestoClient` has the same interface (`cards`, `trips`, `orders`, context
manager) — `from transitlog import PrestoClient` and use it the same way, with
`resolve_credentials("presto")`. It raises `TransitError` from `receipt()` and
`statements()`, which PRESTO doesn't provide.

Records are dataclasses (`Card`, `Trip`, `Order`) with `Decimal` money and
timezone-aware datetimes. Use `.to_dict()` for JSON-friendly values.

Failures raise `TransitError` (or `LoginFailed`). `str(e)` explains the problem
and `e.raw` holds the original response or exception.

## Development

```bash
uv venv .venv && uv pip install --python .venv/bin/python -e '.[dev]'
.venv/bin/pytest
```

The tests run offline against a fake Clipper site that reproduces the quirks
above. They need no account.

## License

Copyright (c) 2026 Amol Venkataraman.

Released under the MIT License — do what you like with it, keep the copyright and
licence notice, and note that it comes with **no warranty of any kind**. See
[LICENSE](LICENSE) and [DISCLAIMER.md](DISCLAIMER.md).

Not affiliated with or endorsed by Clipper, the Metropolitan Transportation
Commission, PRESTO, Metrolinx or any transit operator. "Clipper" and "PRESTO"
are trademarks of their respective owners.
