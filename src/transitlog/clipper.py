# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Amol Venkataraman
"""Read-only client for the Clipper (San Francisco Bay Area) account website.

clippercard.com has no public API. This client logs in like the web form does
and uses the site's own "Download Trip History" / "Download Order History"
exports, which return CSV. Those exports have quirks the client works around:

- An export only covers the filter last applied in the session, so every
  download is preceded by a filter request for one card and one date window.
- Exports stop at 100 rows (newest first) without saying so. Windows that come
  back with 100 rows are split in half and fetched again.
- Window edges are not interpreted in Pacific time (a "July 1" window can
  include a June 30 late-evening trip), so each window is padded by a day and
  results are de-duplicated by ID and trimmed to the requested dates locally.
- If a trip filter finds nothing, the session keeps its previous filter and
  the export silently returns that older data. The client checks that the
  filter page shows the requested period before every export, and checks that
  exported rows fall inside it.
- The orders page only ever lists the last 90 days and says "No orders found"
  for older windows, but the filter is still applied and the export has the
  full history, so the period label (not the empty message) decides.
- An empty export is a zero-byte body with no header row.
- The site's "All Cards" filter option fails server-side, so cards are queried
  one at a time.
- Ranges longer than a year return "no trips"; windows here are at most a month.

The same logged-in session can reach checkout, card removal and fare-dispute
forms. `_request` only allows the endpoints listed in ALLOWED, so none of those
can be reached from this client.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

import requests

from . import __version__
from .config import Credentials
from .errors import LoginFailed, TransitError, UnsafeRequest, explain_network
from .models import Card, Order, Trip

BASE_URL = "https://www.clippercard.com"
SITE = "clippercard.com"
TZ = ZoneInfo("America/Los_Angeles")
USER_AGENT = f"transitlog/{__version__} (+https://github.com/upstream-wontfix/transitlog)"
EXPORT_ROW_CAP = 100
MAX_WINDOW_DAYS = 31
REQUEST_GAP = 0.5  # seconds between requests; the site sits behind bot protection
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December"]

ALLOWED: set[tuple[str, str]] = {
    ("GET", "/web-login"),
    ("POST", "/dashboard"),            # the login form's action; also renders the dashboard
    ("GET", "/dashboard"),
    ("POST", "/view/logout"),
    ("POST", "/loadFilteredTripHistory"),
    ("POST", "/loadFilteredOrderHistory"),
    ("POST", "/download-trip-history/CSV"),
    ("POST", "/download-trip-history/PDF"),
    ("POST", "/download-order-history/CSV"),
}
RECEIPT_PATH_RE = re.compile(r"^/download-print-receipt/\d+$")

CSRF_RE = re.compile(r'name="_csrf"\s+value="([^"]+)"')
FILTER_PERIOD_RE = re.compile(r'id="filterPeriod">\s*([^<]*?)\s*<')
AUTH_FAIL_RE = re.compile(r'id="authFailCount"[^>]*value="(\d+)"')
PATRON_MARKER = "var patronDetails = "

TRIP_COLUMNS = ["ACCOUNT NUMBER", "END DATE/TIME", "END LOCATION", "FARE", "OPERATOR", "PASS",
                "START DATE/TIME", "START LOCATION", "TRIP ID"]
ORDER_COLUMNS = ["ACCOUNT NUMBER", "AMOUNT", "DATE", "ORDER #", "ORDER STATUS", "ORDER TYPE",
                 "PAYMENT", "PAYMENT #", "PRODUCTS"]


@dataclass(frozen=True)
class _HistoryKind:
    name: str
    filter_path: str
    download_path: str
    empty_marker: str
    columns: list[str]
    id_column: str
    date_column: str


TRIPS = _HistoryKind("trips", "/loadFilteredTripHistory", "/download-trip-history", "No trips found",
                     TRIP_COLUMNS, "TRIP ID", "START DATE/TIME")
ORDERS = _HistoryKind("orders", "/loadFilteredOrderHistory", "/download-order-history", "No orders found",
                      ORDER_COLUMNS, "ORDER #", "DATE")


# --------------------------------------------------------------------------- parsing helpers

def period_label(start: date, end: date) -> str:
    """The text the site shows for a custom range, e.g. 'June 1, 2026 - June 15, 2026'."""
    def one(d: date) -> str:
        return f"{MONTHS[d.month - 1]} {d.day}, {d.year}"
    return f"{one(start)} - {one(end)}"


def _blank(value: str | None) -> str | None:
    value = (value or "").strip()
    return None if value in {"", "N/A", "null", "NONE"} else value  # NONE: a Muni bus tap with no stop recorded


def parse_money(value: str) -> Decimal:
    text = value.strip().replace("$", "").replace(",", "")
    try:
        return Decimal(text or "0")
    except InvalidOperation:
        raise TransitError(f"unexpected amount {value!r} in Clipper export") from None


def parse_time(value: str | None) -> datetime | None:
    value = _blank(value)
    if value is None:
        return None
    try:
        return datetime.strptime(value, "%m/%d/%Y %H:%M:%S").replace(tzinfo=TZ)
    except ValueError:
        raise TransitError(f"unexpected date {value!r} in Clipper export") from None


def parse_csv(text: str, columns: list[str]) -> list[dict[str, str]]:
    if not text.strip():
        return []
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    if reader.fieldnames is None or not set(columns) <= set(reader.fieldnames):
        raise TransitError("Clipper's export didn't have the expected columns; the site may have changed",
                           raw=text[:500])
    return list(reader)


def trip_from_row(row: dict[str, str]) -> Trip:
    start = parse_time(row["START DATE/TIME"])
    if start is None:
        raise TransitError(f"trip {row.get('TRIP ID')} has no start time", raw=row)
    fare = parse_money(row["FARE"])
    product = _blank(row["PASS"])
    return Trip(
        provider="clipper",
        trip_id=row["TRIP ID"].strip(),
        account_id=row["ACCOUNT NUMBER"].strip(),
        operator=row["OPERATOR"].strip(),
        start_time=start,
        start_location=_blank(row["START LOCATION"]),
        end_time=parse_time(row["END DATE/TIME"]),
        end_location=_blank(row["END LOCATION"]),
        fare=fare,
        product=product,
        # Clipper doesn't tap each end separately or report transfers explicitly; a $0 ride with no
        # pass is its free-transfer signal. It gives no transfer-credit amount, so that stays unset.
        tap=None,
        transfer=fare == 0 and product is None,
        transfer_credit=None,
    )


def order_from_row(row: dict[str, str]) -> Order:
    when = parse_time(row["DATE"])
    if when is None:
        raise TransitError(f"order {row.get('ORDER #')} has no date", raw=row)
    return Order(
        provider="clipper",
        order_id=row["ORDER #"].strip(),
        account_id=row["ACCOUNT NUMBER"].strip(),
        time=when,
        amount=parse_money(row["AMOUNT"]),
        status=_blank(row["ORDER STATUS"]),
        order_type=_blank(row["ORDER TYPE"]),
        payment=_blank(row["PAYMENT"]),
        payment_ref=_blank(row["PAYMENT #"]),
        products=[p.strip() for p in row["PRODUCTS"].split(";") if p.strip()],
    )


def parse_patron(html: str) -> dict[str, Any]:
    """Extract the account JSON the dashboard embeds as `var patronDetails = {...}`."""
    start = html.find(PATRON_MARKER)
    if start < 0:
        raise TransitError("couldn't find card data on the Clipper dashboard; the site may have changed")
    try:
        obj, _ = json.JSONDecoder().raw_decode(html, start + len(PATRON_MARKER))
    except json.JSONDecodeError as e:
        raise TransitError("couldn't parse card data on the Clipper dashboard", raw=e) from e
    return obj


def _cents(value: Any) -> Decimal | None:
    return None if value is None else Decimal(int(value)) / 100


def cards_from_patron(patron: dict[str, Any]) -> list[Card]:
    customer = patron.get("currentCustomer") or patron.get("primaryCustomer") or {}
    cards = []
    for c in customer.get("displayCardList") or []:
        token = c.get("accountTokenVO") or {}
        info = token.get("tokenInfo") or {}
        passes = []
        for key, value in c.items():  # pass lists are null on cards without passes; their key name is unconfirmed
            if "pass" in key.lower() and isinstance(value, list):
                passes += [str(p.get("name") or p.get("description") or p) if isinstance(p, dict) else str(p)
                           for p in value]
        cards.append(Card(
            provider="clipper",
            account_id=str(c.get("subsystemAccountReference") or ""),
            card_number=info.get("serialNumber"),
            nickname=c.get("nickname") or info.get("tokenNickname"),
            status=token.get("statusDescription") or token.get("status"),
            rider_class=c.get("riderClassDescription"),
            cash_balance=_cents(c.get("consolidatedPurseBalance", (c.get("cashPurse") or {}).get("balance"))),
            passes=passes,
            autoload=bool((c.get("cashPurse") or {}).get("autoloadActive")) or bool(c.get("autoloadValue")),
            activated=token.get("tokenActivatedDateTime"),
            expires=info.get("cardExpiryDate"),
        ))
    return cards


def month_windows(since: date, until: date, max_days: int = MAX_WINDOW_DAYS) -> Iterator[tuple[date, date]]:
    """Split [since, until] into calendar-month windows (never longer than max_days)."""
    start = since
    while start <= until:
        next_month = (start.replace(day=1) + timedelta(days=32)).replace(day=1)
        end = min(until, next_month - timedelta(days=1), start + timedelta(days=max_days - 1))
        yield start, end
        start = end + timedelta(days=1)


def collect_windows(fetch: Callable[[date, date], list[dict[str, str]]], since: date, until: date,
                    cap: int = EXPORT_ROW_CAP, warn: Callable[[str], None] = lambda _: None,
                    ) -> tuple[list[dict[str, str]], list[tuple[date, date]]]:
    """Fetch every window, halving any that hit the export cap.

    Returns all rows (possibly with duplicates) and the windows that were finally used.
    """
    pending = list(month_windows(since, until))
    rows: list[dict[str, str]] = []
    used: list[tuple[date, date]] = []
    while pending:
        start, end = pending.pop(0)
        got = fetch(start, end)
        if len(got) >= cap and start < end:
            mid = start + (end - start) // 2
            pending[:0] = [(start, mid), (mid + timedelta(days=1), end)]
            continue
        if len(got) >= cap:
            warn(f"{start} has {len(got)}+ records; Clipper's export may have cut some off")
        rows += got
        used.append((start, end))
    return rows, used


# --------------------------------------------------------------------------- client

class ClipperClient:
    """Log in to clippercard.com and read cards, trips, orders and receipts.

    Use as a context manager; it logs in on entry and out on exit.
    """

    def __init__(self, credentials: Credentials, timeout: int = 30, logger: logging.Logger | None = None,
                 pause: float = REQUEST_GAP, session: requests.Session | None = None):
        self.credentials = credentials
        self.timeout = timeout
        self.log = logger or logging.getLogger("transitlog.clipper")
        self.pause = pause
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self._csrf: str | None = None
        self._patron: dict[str, Any] | None = None
        self._last_request = 0.0
        self._trip_windows: dict[tuple[str, date, date], list[tuple[date, date]]] = {}
        self.warnings: list[str] = []

    def __enter__(self) -> ClipperClient:
        self.login()
        return self

    def __exit__(self, *exc: object) -> None:
        self.logout()

    # ----------------------------------------------------------------- transport

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        if (method, path) not in ALLOWED and not (method == "GET" and RECEIPT_PATH_RE.match(path)):
            raise UnsafeRequest(f"refusing {method} {path}: not on the read-only allowlist")
        wait = self._last_request + self.pause - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        try:
            r = self.session.request(method, BASE_URL + path, timeout=self.timeout, **kwargs)
        except requests.RequestException as e:
            raise TransitError(explain_network(e, SITE), raw=e) from e
        finally:
            self._last_request = time.monotonic()
        self.log.debug("%s %s -> %s %s (%d bytes)", method, path, r.status_code, r.headers.get("content-type"),
                       len(r.content))
        if "_Incapsula_Resource" in r.text[:2000] and "Request unsuccessful" in r.text[:2000]:
            raise TransitError(f"{SITE}'s bot protection blocked the request; wait a few minutes and retry",
                               raw=r.text[:500])
        if r.status_code >= 400:
            raise TransitError(f"{SITE} returned HTTP {r.status_code} for {path}", raw=r.text[:500])
        return r

    def _csrf_headers(self) -> dict[str, str]:
        if not self._csrf:
            raise TransitError("not logged in")
        return {"X-CSRF-TOKEN": self._csrf}

    def _check_session(self, r: requests.Response) -> None:
        if r.url.rstrip("/").endswith("/web-login") or 'id="validate-login-form"' in r.text:
            raise TransitError("the Clipper session expired; run the command again")

    # ----------------------------------------------------------------- session

    def login(self) -> None:
        page = self._request("GET", "/web-login")
        token = CSRF_RE.search(page.text)
        if not token:
            raise TransitError("couldn't find the login form on clippercard.com; the site may have changed")
        r = self._request("POST", "/dashboard", data={
            "_csrf": token.group(1), "authFailCount": "0", "postLoginUrl": "",
            "username": self.credentials.username, "password": self.credentials.password,
        })
        if PATRON_MARKER not in r.text:
            fails = AUTH_FAIL_RE.search(r.text)
            count = f" ({fails.group(1)} failed attempt(s) so far)" if fails and fails.group(1) != "0" else ""
            raise LoginFailed(f"Clipper rejected the login for {self.credentials.username}{count}; check the "
                              "username and password file. Repeated failures can lock the account, so this "
                              "tool never retries on its own.")
        self._patron = parse_patron(r.text)
        token = CSRF_RE.search(r.text)
        self._csrf = token.group(1) if token else None

    def logout(self) -> None:
        if not self._csrf:
            return
        try:
            self._request("POST", "/view/logout", data={"_csrf": self._csrf})
        except TransitError as e:  # best effort; the session expires on its own
            self.log.debug("logout failed: %s", e)
        self._csrf = None

    # ----------------------------------------------------------------- data

    def cards(self) -> list[Card]:
        if self._patron is None:
            raise TransitError("not logged in")
        return cards_from_patron(self._patron)

    def select_cards(self, selector: str | None) -> list[Card]:
        """All cards, or the one whose account number, card number or last four digits match."""
        cards = self.cards()
        if not selector:
            return cards
        found = [c for c in cards if selector in {c.account_id, c.card_number}
                 or (len(selector) == 4 and (c.card_number or "").endswith(selector))]
        if not found:
            known = ", ".join(c.label for c in cards) or "none"
            raise TransitError(f"no card matches {selector!r} (cards on this account: {known})")
        return found

    def _apply_filter(self, kind: _HistoryKind, account: str, start: date, end: date) -> str | None:
        """Set the session's history filter. Returns the period label, or None if the window is empty."""
        r = self._request("POST", kind.filter_path, data={
            "_csrf": self._csrf, "transitAccountId": account, "dateRange": "Custom Date Range",
            "startDate": start.isoformat(), "endDate": end.isoformat(),
        })
        self._check_session(r)
        expected = period_label(start, end)
        shown = FILTER_PERIOD_RE.search(r.text)
        if shown and shown.group(1) == expected:
            return expected
        if kind.empty_marker in r.text:
            return None
        # Exporting now would return whatever filter the session had before.
        raise TransitError(f"Clipper didn't apply the {kind.name} filter for {expected} "
                           f"(page shows {shown.group(1) if shown else 'no filter'!r}); not exporting",
                           raw=r.text[:500])

    def _export_window(self, kind: _HistoryKind, account: str, start: date, end: date) -> list[dict[str, str]]:
        # Pad by a day each side: the site's window edges aren't in Pacific time.
        qstart, qend = start - timedelta(days=1), end + timedelta(days=1)
        label = self._apply_filter(kind, account, qstart, qend)
        if label is None:
            return []
        r = self._request("POST", f"{kind.download_path}/CSV",
                          data=json.dumps({"filterPeriod": label, "filterTA": account}),
                          headers={**self._csrf_headers(), "Content-Type": "application/json;charset=UTF-8"})
        rows = parse_csv(r.text, kind.columns)
        for row in rows:
            when = parse_time(row[kind.date_column])
            if row["ACCOUNT NUMBER"].strip() != account or when is None \
                    or not qstart - timedelta(days=1) <= when.date() <= qend + timedelta(days=1):
                raise TransitError(f"Clipper's {kind.name} export returned records outside {label} for "
                                   f"account {account}; not saving possibly stale data", raw=row)
        self.log.debug("%s %s %s..%s: %d rows", kind.name, account, start, end, len(rows))
        return rows

    def _history(self, kind: _HistoryKind, since: date, until: date, card: str | None) -> list[dict[str, str]]:
        if since > until:
            raise TransitError(f"start date {since} is after end date {until}")
        rows: dict[str, dict[str, str]] = {}
        for c in self.select_cards(card):
            got, used = collect_windows(lambda a, b, acct=c.account_id: self._export_window(kind, acct, a, b),
                                        since, until, warn=self.warnings.append)
            if kind is TRIPS:
                self._trip_windows[(c.account_id, since, until)] = used
            for row in got:
                rows.setdefault(row[kind.id_column].strip(), row)
        return [row for row in rows.values() if since <= parse_time(row[kind.date_column]).date() <= until]

    def trips(self, since: date, until: date, card: str | None = None) -> list[Trip]:
        """Every trip that started between `since` and `until` (inclusive, Pacific time), oldest first."""
        trips = [trip_from_row(r) for r in self._history(TRIPS, since, until, card)]
        return sorted(trips, key=lambda t: (t.start_time, t.trip_id))

    def orders(self, since: date, until: date, card: str | None = None) -> list[Order]:
        """Every order (cash-value loads, pass purchases, card fees) in the range, oldest first."""
        orders = [order_from_row(r) for r in self._history(ORDERS, since, until, card)]
        return sorted(orders, key=lambda o: (o.time, o.order_id))

    def receipt(self, order_id: str) -> bytes:
        """The site's PDF receipt for one order."""
        if not order_id.isdigit():
            raise TransitError(f"invalid order number {order_id!r}")
        r = self._request("GET", f"/download-print-receipt/{order_id}", headers=self._csrf_headers())
        if not r.content.startswith(b"%PDF"):
            raise TransitError(f"Clipper didn't return a PDF receipt for order {order_id}", raw=r.text[:300])
        return r.content

    def statements(self, since: date, until: date, card: str | None = None) -> Iterator[tuple[Card, date, date, bytes]]:
        """Clipper's official trip-history PDFs, one per window small enough to fit under the export cap."""
        for c in self.select_cards(card):
            windows = self._trip_windows.get((c.account_id, since, until))
            if windows is None:  # not already planned by trips() for this range
                _, windows = collect_windows(lambda a, b, acct=c.account_id: self._export_window(TRIPS, acct, a, b),
                                             since, until, warn=self.warnings.append)
            for start, end in windows:
                label = self._apply_filter(TRIPS, c.account_id, start, end)
                if label is None:
                    continue
                r = self._request("POST", "/download-trip-history/PDF",
                                  data=json.dumps({"filterPeriod": label, "filterTA": c.account_id}),
                                  headers={**self._csrf_headers(), "Content-Type": "application/json;charset=UTF-8"})
                if not r.content.startswith(b"%PDF"):
                    raise TransitError(f"Clipper didn't return a PDF for {label}", raw=r.text[:300])
                yield c, start, end, r.content
