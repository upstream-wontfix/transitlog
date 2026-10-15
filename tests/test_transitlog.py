# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Amol Venkataraman
#
# All account, card and order numbers below are made up. FakeClipper imitates
# the clippercard.com behaviours the client depends on (see clipper.py's
# docstring): the 100-row export cap, trip filters that silently keep the old
# filter when nothing matches, the orders page that hides old orders, and
# zero-byte empty exports.
import csv
import io
import itertools
import json
import os
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from transitlog import __version__, cli
from transitlog.clipper import (
    EXPORT_ROW_CAP,
    ORDER_COLUMNS,
    TRIP_COLUMNS,
    TZ,
    ClipperClient,
    cards_from_patron,
    collect_windows,
    month_windows,
    order_from_row,
    parse_csv,
    period_label,
    trip_from_row,
)
from transitlog.config import (
    ConfigError,
    Credentials,
    read_password_file,
    resolve_credentials,
    write_password_file,
)
from transitlog.errors import LoginFailed, TransitError, UnsafeRequest
from transitlog.models import Order, Trip
from transitlog.report import build_report, paid_with, render_report

ACCOUNT = "100000000001"
SERIAL = "1200000001"
CREDS = Credentials("rider@example.com", "not-a-real-password")


# --------------------------------------------------------------------------- fake site

def patron_json(balance_cents: int = 1250) -> dict:
    return {"currentCustomer": {"displayCardList": [{
        "nickname": "Commute card", "subsystemAccountReference": ACCOUNT, "riderClassDescription": "Adult",
        "consolidatedPurseBalance": balance_cents, "cashPurse": {"balance": balance_cents, "autoloadActive": False},
        "autoloadValue": 0, "passList": [{"name": "Muni-Only Pass (Adult)"}],
        "accountTokenVO": {"statusDescription": "Active", "tokenActivatedDateTime": "2026-05-07T11:16:22.000-07:00",
                           "tokenInfo": {"serialNumber": SERIAL, "cardExpiryDate": "2037-12-31T15:59:59.000-08:00"}},
    }]}}


def fmt(dt: datetime) -> str:
    return dt.strftime("%m/%d/%Y %H:%M:%S")


def make_trip_row(i: int, when: datetime) -> dict:
    return {"ACCOUNT NUMBER": ACCOUNT, "END DATE/TIME": "N/A", "END LOCATION": "N/A", "FARE": "$0.00",
            "OPERATOR": "Muni", "PASS": "Muni-Only Pass (Adult)", "START DATE/TIME": fmt(when),
            "START LOCATION": "Church", "TRIP ID": str(50000000 + i)}


def make_order_row(i: int, when: datetime, amount: str = "$20.00", products: str = "Cash Value; ") -> dict:
    return {"ACCOUNT NUMBER": ACCOUNT, "AMOUNT": amount, "DATE": fmt(when), "ORDER #": str(60000000 + i),
            "ORDER STATUS": "Completed", "ORDER TYPE": "Sale", "PAYMENT": "null", "PAYMENT #": f"PRN{i}",
            "PRODUCTS": products}


def to_csv(rows: list[dict], columns: list[str]) -> str:
    if not rows:
        return ""
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=columns, quoting=csv.QUOTE_ALL)
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


class FakeResponse:
    def __init__(self, url: str, text: str = "", status: int = 200, content: bytes | None = None,
                 content_type: str = "text/html"):
        self.url, self.text, self.status_code = url, text, status
        self.content = content if content is not None else text.encode()
        self.headers = {"content-type": content_type}


class FakeClipper:
    """Stands in for requests.Session against clippercard.com."""

    def __init__(self, trips=(), orders=(), password="not-a-real-password", today=date(2026, 10, 14)):
        self.trips, self.orders, self.password, self.today = list(trips), list(orders), password, today
        self.headers: dict = {}
        self.filter = {"trips": None, "orders": None}  # (account, start, end) the session will export
        self.log: list[tuple[str, str]] = []
        self.stale_trip_rows: list[dict] = []          # what an export returns before any filter matched

    def _page(self, body: str) -> str:
        return f'<html><input type="hidden" name="_csrf" value="tok"/>{body}</html>'

    def request(self, method, url, timeout=None, data=None, headers=None, **_):
        path = url.replace("https://www.clippercard.com", "")
        self.log.append((method, path))
        if path == "/web-login":
            return FakeResponse(url, self._page('<form id="validate-login-form"></form>'))
        if path == "/dashboard":
            if data["password"] != self.password:
                return FakeResponse(url, self._page(
                    '<form id="validate-login-form"><input id="authFailCount" value="1"></form>'))
            return FakeResponse(url, self._page(f"<script>var patronDetails = {json.dumps(patron_json())};</script>"))
        if path == "/view/logout":
            return FakeResponse(url, self._page(""))
        if path in ("/loadFilteredTripHistory", "/loadFilteredOrderHistory"):
            kind = "trips" if "Trip" in path else "orders"
            start, end = date.fromisoformat(data["startDate"]), date.fromisoformat(data["endDate"])
            label = period_label(start, end)
            rows = self._rows(kind, data["transitAccountId"], start, end)
            if kind == "trips":
                if not rows:  # filter not applied; label shown without its id
                    return FakeResponse(url, self._page(f"<label>{label}</label> No trips found"))
                self.filter[kind] = (data["transitAccountId"], start, end)
                return FakeResponse(url, self._page(f'<label id="filterPeriod">{label}</label>'))
            self.filter[kind] = (data["transitAccountId"], start, end)
            # the orders page only lists the last 90 days, whatever the filter says
            recent = [r for r in rows if self._when(r, "DATE").date() > self.today - timedelta(days=90)]
            return FakeResponse(url, self._page(f'<label id="filterPeriod">{label}</label>'
                                                + ("" if recent else " No orders found")))
        if path.startswith("/download-trip-history/") or path.startswith("/download-order-history/"):
            kind = "trips" if "trip" in path else "orders"
            if self.filter[kind] is None:
                rows = self.stale_trip_rows if kind == "trips" else []
            else:
                rows = self._rows(kind, *self.filter[kind])
            date_col = "START DATE/TIME" if kind == "trips" else "DATE"
            rows = sorted(rows, key=lambda r: self._when(r, date_col), reverse=True)[:EXPORT_ROW_CAP]
            if path.endswith("/PDF"):
                return FakeResponse(url, content=b"%PDF-1.7 " + str(len(rows)).encode(), content_type="application/pdf")
            return FakeResponse(url, to_csv(rows, TRIP_COLUMNS if kind == "trips" else ORDER_COLUMNS),
                                content_type="text/plain")
        if path.startswith("/download-print-receipt/"):
            if path.endswith("0"):
                return FakeResponse(url, '{"status":500}', status=500)
            return FakeResponse(url, content=b"%PDF-1.7 receipt", content_type="application/octet-stream")
        return FakeResponse(url, "not found", status=404)

    @staticmethod
    def _when(row, col):
        return datetime.strptime(row[col], "%m/%d/%Y %H:%M:%S")

    def _rows(self, kind, account, start, end):
        rows, col = (self.trips, "START DATE/TIME") if kind == "trips" else (self.orders, "DATE")
        return [r for r in rows if r["ACCOUNT NUMBER"] == account and start <= self._when(r, col).date() <= end]


def client_for(site: FakeClipper) -> ClipperClient:
    return ClipperClient(CREDS, session=site, pause=0)


def daily_trips(start: date, days: int, per_day: int) -> list[dict]:
    rows, i = [], 0
    for d in range(days):
        for k in range(per_day):
            rows.append(make_trip_row(i, datetime.combine(start + timedelta(days=d), datetime.min.time())
                                      + timedelta(hours=7, minutes=10 * k)))
            i += 1
    return rows


# --------------------------------------------------------------------------- config

def test_password_file_roundtrip_is_private(tmp_path):
    path = write_password_file("p@ss word!", tmp_path / "sub" / "pw")
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert read_password_file(path) == "p@ss word!"


def test_world_readable_password_file_warns(tmp_path, capsys):
    pw = tmp_path / "pw"
    pw.write_text("secret\n")
    os.chmod(pw, 0o644)
    assert read_password_file(pw) == "secret"
    assert "chmod 600" in capsys.readouterr().err


def test_credentials_precedence(tmp_path):
    cfg, pw_cfg, pw_flag = tmp_path / "config.ini", tmp_path / "pw_cfg", tmp_path / "pw_flag"
    write_password_file("from-config-file", pw_cfg)
    write_password_file("from-flag-file", pw_flag)
    cfg.write_text(f"[clipper]\nusername = cfg@example.com\npassword_file = {pw_cfg}\n")

    assert resolve_credentials("clipper", config_file=cfg, env={}) == Credentials("cfg@example.com", "from-config-file")
    env = {"TRANSITLOG_CLIPPER_USERNAME": "env@example.com", "TRANSITLOG_CLIPPER_PASSWORD": "from-env"}
    assert resolve_credentials("clipper", config_file=cfg, env=env) == Credentials("env@example.com", "from-env")
    # an explicit password file beats the environment password
    c = resolve_credentials("clipper", "flag@example.com", pw_flag, config_file=cfg, env=env)
    assert c == Credentials("flag@example.com", "from-flag-file")
    assert "from-flag-file" not in repr(c)


def test_missing_username_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match="no clipper username"):
        resolve_credentials("clipper", config_file=tmp_path / "none.ini", env={"TRANSITLOG_CLIPPER_PASSWORD": "x"})


# --------------------------------------------------------------------------- parsing

def test_parse_csv_empty_and_wrong_columns():
    assert parse_csv("", TRIP_COLUMNS) == []
    with pytest.raises(TransitError, match="expected columns"):
        parse_csv('"SOMETHING","ELSE"\n"1","2"\n', TRIP_COLUMNS)


def test_trip_and_order_rows():
    row = make_trip_row(1, datetime(2026, 6, 30, 23, 3, 36)) | {
        "END DATE/TIME": "07/01/2026 00:39:31", "END LOCATION": "Civic Center", "FARE": "$4.70",
        "OPERATOR": "BART", "PASS": "Cash Value ", "START LOCATION": "NONE"}
    t = trip_from_row(row)
    assert (t.fare, t.product) == (Decimal("4.70"), "Cash Value")
    assert (t.start_location, t.end_location) == (None, "Civic Center")
    assert t.start_time.tzinfo == TZ and t.end_time.date() == date(2026, 7, 1)

    o = order_from_row(make_order_row(1, datetime(2026, 5, 7, 11, 16), "$20.00", "Cash Value; Adult Clipper Card; "))
    assert (o.amount, o.payment, o.products) == (Decimal("20.00"), None, ["Cash Value", "Adult Clipper Card"])


def test_period_label_matches_site_format():
    assert period_label(date(2026, 6, 1), date(2026, 6, 15)) == "June 1, 2026 - June 15, 2026"


def test_month_windows():
    windows = list(month_windows(date(2025, 10, 15), date(2026, 1, 3)))
    assert windows == [(date(2025, 10, 15), date(2025, 10, 31)), (date(2025, 11, 1), date(2025, 11, 30)),
                       (date(2025, 12, 1), date(2025, 12, 31)), (date(2026, 1, 1), date(2026, 1, 3))]


def test_collect_windows_splits_capped_windows():
    per_day = {date(2026, 6, 1) + timedelta(days=i): 6 for i in range(30)}  # 180 in June

    def fetch(a, b):
        return [{}] * min(EXPORT_ROW_CAP, sum(n for d, n in per_day.items() if a <= d <= b))

    rows, used = collect_windows(fetch, date(2026, 6, 1), date(2026, 6, 30))
    assert len(rows) == 180
    assert used == [(date(2026, 6, 1), date(2026, 6, 15)), (date(2026, 6, 16), date(2026, 6, 30))]


def test_collect_windows_warns_when_one_day_is_capped():
    warnings = []
    rows, _ = collect_windows(lambda a, b: [{}] * EXPORT_ROW_CAP, date(2026, 6, 1), date(2026, 6, 1),
                              warn=warnings.append)
    assert len(rows) == EXPORT_ROW_CAP and "cut some off" in warnings[0]


def test_cards_from_patron():
    [card] = cards_from_patron(patron_json(950))
    assert (card.account_id, card.card_number, card.status) == (ACCOUNT, SERIAL, "Active")
    assert card.cash_balance == Decimal("9.50")
    assert card.passes == ["Muni-Only Pass (Adult)"] and card.label == "Commute card (0001)"


# --------------------------------------------------------------------------- client

def test_requests_outside_allowlist_are_refused():
    site = FakeClipper()
    c = client_for(site)
    for method, path in [("POST", "/wsLinkClipperCardResponse"), ("POST", "/download-print-receipt/1"),
                         ("GET", "/download-print-receipt/1/../../settings"), ("GET", "/settings")]:
        with pytest.raises(UnsafeRequest):
            c._request(method, path)
    assert site.log == []


def test_wrong_password_raises_login_failed_once():
    site = FakeClipper(password="something-else")
    with pytest.raises(LoginFailed, match="1 failed attempt"):
        client_for(site).login()
    assert site.log.count(("POST", "/dashboard")) == 1


def test_trips_across_cap_and_windows_are_complete_and_deduplicated():
    rows = daily_trips(date(2026, 5, 10), 60, 5)  # 300 trips, ~150 a month
    with client_for(FakeClipper(trips=rows)) as c:
        trips = c.trips(date(2026, 5, 1), date(2026, 7, 31))
        assert sorted(t.trip_id for t in trips) == sorted(r["TRIP ID"] for r in rows)
        assert trips == sorted(trips, key=lambda t: t.start_time)
        # trimmed to the requested dates even though windows are padded
        inner = c.trips(date(2026, 6, 1), date(2026, 6, 2))
        assert {t.start_time.date() for t in inner} == {date(2026, 6, 1), date(2026, 6, 2)} and len(inner) == 10


def test_empty_trip_window_never_exports_stale_filter():
    site = FakeClipper(trips=daily_trips(date(2026, 8, 1), 3, 2))
    site.stale_trip_rows = daily_trips(date(2026, 8, 1), 3, 2)  # what the site would leak if exported
    with client_for(site) as c:
        assert c.trips(date(2026, 9, 1), date(2026, 9, 30)) == []
    assert not any(p.startswith("/download-trip-history") for _, p in site.log)


def test_stale_rows_outside_window_are_rejected():
    class StaleExport(FakeClipper):
        def request(self, method, url, **kw):
            if "/download-trip-history/" in url:  # ignores the filter, returns January
                return FakeResponse(url, to_csv(daily_trips(date(2026, 1, 1), 1, 1), TRIP_COLUMNS))
            return super().request(method, url, **kw)

    with client_for(StaleExport(trips=daily_trips(date(2026, 8, 1), 3, 2))) as c:
        with pytest.raises(TransitError, match="outside"):
            c.trips(date(2026, 8, 1), date(2026, 8, 3))


def test_old_orders_are_exported_although_the_page_hides_them():
    orders = [make_order_row(1, datetime(2026, 5, 7, 11, 16)), make_order_row(2, datetime(2026, 8, 28, 11, 16))]
    with client_for(FakeClipper(orders=orders)) as c:
        got = c.orders(date(2026, 1, 1), date(2026, 10, 14))
    assert [o.order_id for o in got] == ["60000001", "60000002"]


def test_select_cards_and_unknown_card():
    with client_for(FakeClipper()) as c:
        assert [x.account_id for x in c.select_cards("0001")] == [ACCOUNT]
        assert c.select_cards(SERIAL) == c.select_cards(ACCOUNT)
        with pytest.raises(TransitError, match="no card matches"):
            c.select_cards("9999")


def test_logout_on_exit():
    site = FakeClipper()
    with client_for(site):
        pass
    assert site.log[-1] == ("POST", "/view/logout")


# --------------------------------------------------------------------------- report

TRIP_IDS = itertools.count(1)


def trip(op, when, fare="0", product=None, a=None, b=None):
    return Trip("clipper", str(next(TRIP_IDS)), ACCOUNT, op, when, a, None, b, Decimal(fare), product)


def test_paid_with():
    now = datetime(2026, 6, 1, 8, tzinfo=TZ)
    assert paid_with(trip("Muni", now, product="Muni-Only Pass (Adult)")) == "pass"
    assert paid_with(trip("BART", now, "5.25", "Cash Value")) == "cash value"
    assert paid_with(trip("Muni", now)) == "free"


def test_report_numbers():
    d = datetime(2026, 6, 1, 8, tzinfo=TZ)
    trips = [trip("Muni", d, product="Muni-Only Pass (Adult)", a="Church"),
             trip("Muni", d + timedelta(hours=10), product="Muni-Only Pass (Adult)", a="Church"),
             trip("BART", d + timedelta(days=1), "5.25", "Cash Value", "Embarcadero", "Dublin / Pleasanton")]
    orders = [Order("clipper", "1", ACCOUNT, d - timedelta(days=1), Decimal("86.00"), "Completed", "Sale", None, None,
                    ["Muni-Only Pass (Adult)"]),
              Order("clipper", "2", ACCOUNT, d, Decimal("20.00"), "Completed", "Sale", None, None, ["Cash Value"])]
    r = build_report(trips, orders, date(2026, 5, 1), date(2026, 6, 30))
    assert r["trips"]["total"] == 3 and r["trips"]["active_days"] == 2
    assert r["by_operator"][0] == {"operator": "Muni", "trips": 2, "fares": Decimal(0)}
    assert r["passes"] == [{"pass": "Muni-Only Pass (Adult)", "purchases": 1, "cost": Decimal("86.00"), "rides": 2,
                            "cost_per_ride": Decimal("43.00")}]
    assert r["spending"]["orders_total"] == Decimal("106.00")
    assert r["top_journeys"] == [{"from": "Embarcadero", "to": "Dublin / Pleasanton", "trips": 1}]
    text = render_report(r)
    assert "Muni-Only Pass (Adult)" in text and "$43.00" in text
    assert "No trips" in render_report(build_report([], [], date(2026, 1, 1), date(2026, 1, 31)))


# --------------------------------------------------------------------------- cli

def test_options_work_before_and_after_subcommands():
    a = cli.parse_args(["--json", "clipper", "trips", "--since", "2026-05-01", "--debug"])
    assert a.json and a.debug and a.since == date(2026, 5, 1) and a.timeout == 30
    with pytest.raises(SystemExit):
        cli.parse_args(["clipper", "trips", "--since", "2026-06-01", "--until", "2026-05-01"])
    with pytest.raises(SystemExit):
        cli.parse_args(["clipper", "trips", "--since", "June 1"])


def test_export_writes_everything(tmp_path, monkeypatch, capsys):
    site = FakeClipper(trips=daily_trips(date(2026, 8, 1), 3, 2),
                       orders=[make_order_row(1, datetime(2026, 8, 1, 9)), make_order_row(10, datetime(2026, 8, 2, 9))])
    monkeypatch.setattr(cli, "ClipperClient", lambda creds, **kw: ClipperClient(creds, session=site, pause=0))
    monkeypatch.setenv("TRANSITLOG_CLIPPER_USERNAME", CREDS.username)
    monkeypatch.setenv("TRANSITLOG_CLIPPER_PASSWORD", CREDS.password)
    out = tmp_path / "export"
    code = cli.main(["clipper", "export", "-d", str(out), "--since", "2026-08-01", "--until", "2026-08-31",
                     "--config", str(tmp_path / "none.ini")])
    assert code == 0
    assert {p.name for p in out.iterdir()} == {"cards.json", "trips.csv", "trips.json", "orders.csv", "orders.json",
                                               "report.txt", "report.json", "receipts"}
    assert len(list(csv.DictReader((out / "trips.csv").open()))) == 6
    assert [p.name for p in (out / "receipts").iterdir()] == ["clipper-receipt-2026-08-01-60000001.pdf"]
    err = capsys.readouterr().err
    assert "1 unavailable" in err and "no receipt for order 60000010" in err


def test_version():
    assert __version__.count(".") == 2


def test_clipper_transfer_inference():
    """Clipper has no explicit transfer flag; a $0 ride with no pass is its free-transfer signal."""
    from transitlog.clipper import trip_from_row as clipper_trip
    base = {"ACCOUNT NUMBER": "100039588023", "OPERATOR": "Muni", "START DATE/TIME": "06/01/2026 09:00:00",
            "START LOCATION": "Church", "END DATE/TIME": "", "END LOCATION": "", "TRIP ID": "t1"}
    free = clipper_trip({**base, "FARE": "$0.00", "PASS": ""})
    assert free.transfer is True and free.transfer_credit is None and free.tap is None
    paid = clipper_trip({**base, "FARE": "$2.50", "PASS": "", "TRIP ID": "t2"})
    assert paid.transfer is False
    on_pass = clipper_trip({**base, "FARE": "$0.00", "PASS": "Muni-Only Pass (Adult)", "TRIP ID": "t3"})
    assert on_pass.transfer is False   # a $0 pass ride is not a transfer
