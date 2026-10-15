# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Amol Venkataraman
#
# All account, card and media numbers below are made up. FakePresto imitates the
# prestocard.ca behaviours the client depends on (see presto.py's docstring): the
# Azure AD B2C sign-in steps (authorize page with a SETTINGS blob, SelfAsserted
# credential POST, the confirmed redirect carrying the auth code, the token
# exchange) and the three read-only CIAM endpoints (account/get, media/get,
# media/getTransactions). It needs no network and no account.
import base64
import json
from datetime import date
from decimal import Decimal

import pytest

from transitlog import PrestoClient
from transitlog.config import Credentials
from transitlog.errors import LoginFailed, TransitError, UnsafeRequest
from transitlog.presto import (
    card_from_instance,
    is_system,
    is_trip,
    jwt_claims,
    order_from_row,
    parse_money,
    parse_time,
    trip_from_row,
)

CREDS = Credentials("rider@example.com", "correct-horse")
ACCOUNT_ID = "1b46bd56-2d8f-4915-bcf0-add79898b3c3"

# Two made-up cards; the first is active with a balance, the second hotlisted.
INSTANCES = [
    {"mediaReferenceId": "123400000001", "mediaInstanceId": "inst-1", "mediaType": "CRDNC",
     "versionNbr": "0", "customName": "Commute", "primary": True, "dpan": "11110001", "fpan": "3124...0001"},
    {"mediaReferenceId": "123400000002", "mediaInstanceId": "inst-2", "mediaType": "CRDNC",
     "versionNbr": "0", "customName": "Spare", "primary": False, "dpan": "11110002", "fpan": "3124...0002"},
]
MEDIA_DETAIL = {"status": "ACCEPTED", "mediaList": [
    {"mediaInfo": {"mediaStatus": "ACTIVE", "visibleCardNumber": "31240011110001001", "mediaInstanceId": "inst-1"},
     "mediaProfile": {"concessions": {"0": {"id": 3, "expirationDate": "2027-02-26T23:59:59.999Z"}}},
     "products": {"funds": 1575, "commutePlans": {}, "subscriptions": []}},
    {"mediaInfo": {"mediaStatus": "HOTLISTED", "visibleCardNumber": "31240011110002001", "mediaInstanceId": "inst-2"},
     "mediaProfile": {}, "products": {"funds": 0, "commutePlans": {}, "subscriptions": []}},
]}
# A tap (Usage), a system event (ActionItem, dropped), and a zero-fare tap.
TRANSACTIONS = {
    "11110001": [
        {"discriminator": "Usage", "transactionTime": "2026-05-25T12:14:20.604Z", "uuid": "tap-1",
         "serviceProviderName": "GO Transit", "locationName": "Union Station Rail", "transactionType": 14,
         "tapType": "TAP_OFF", "tripPrice": 1018, "deductedFare": 648},
        {"discriminator": "ActionItem", "transactionTime": "2026-04-01T20:03:21.817Z", "uuid": "sys-1",
         "transactionType": 11, "actionType": "MIGRATION_COMPLETE"},
        {"discriminator": "Usage", "transactionTime": "2026-05-25T10:42:49.623Z", "uuid": "tap-2",
         "serviceProviderName": "GO Transit", "locationName": "Guildwood GO Station Rail", "transactionType": 14,
         "tapType": "TAP_ON", "tripPrice": 0, "deductedFare": 0},
    ],
    "11110002": [],
}


def _jwt(claims: dict) -> str:
    def seg(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()
    return f"{seg({'alg': 'RS256', 'typ': 'JWT'})}.{seg(claims)}.signature"


class FakeResponse:
    def __init__(self, status=200, text="", headers=None, content=None):
        self.status_code = status
        self.text = text
        self.headers = headers or {}
        self.content = content if content is not None else text.encode()

    def json(self):
        return json.loads(self.text)


class FakePresto:
    """A requests.Session stand-in reproducing the B2C sign-in and the CIAM API."""

    def __init__(self, *, password="correct-horse", with_code=True):
        self.headers = {}
        self.password = password
        self.with_code = with_code
        self.calls = []

    def request(self, method, url, timeout=None, allow_redirects=True, **kw):
        self.calls.append((method, url))
        if url.endswith("/oauth2/v2.0/authorize"):
            return FakeResponse(text='<html>var SETTINGS = {"csrf":"CSRF-TOK","transId":"StateProperties=xyz"};</html>')
        if url.endswith("/SelfAsserted"):
            if kw.get("data", {}).get("password") != self.password:
                return FakeResponse(text=json.dumps({"status": "400", "message": "Invalid credentials"}))
            return FakeResponse(text=json.dumps({"status": "200"}))
        if url.endswith("/CombinedSigninAndSignup/confirmed"):
            loc = "https://prestocard.ca/en/signingin?code=AUTH-CODE&state=x" if self.with_code \
                else "https://prestocard.ca/en/signingin?error=login_required"
            return FakeResponse(status=302, headers={"Location": loc})
        if url.endswith("/oauth2/v2.0/token"):
            claims = {"ticketingAccountId": ACCOUNT_ID, "Customer_Id": "28557485_A24",
                      "medias": [json.dumps(i) for i in INSTANCES]}
            return FakeResponse(text=json.dumps({"access_token": _jwt(claims)}))
        if url.endswith("/account/get"):
            return FakeResponse(text=json.dumps({"account": {"mediaInstances": INSTANCES}}))
        if url.endswith("/media/get"):
            return FakeResponse(text=json.dumps(MEDIA_DETAIL))
        if url.endswith("/media/getTransactions"):
            dpan = json.loads(kw["data"])["mediaInput"]["dpan"]
            return FakeResponse(text=json.dumps({"transactions": TRANSACTIONS.get(dpan, []),
                                                 "continuationToken": None, "status": "ACCEPTED"}))
        raise AssertionError(f"unexpected request {method} {url}")


def client(**kw):
    return PrestoClient(CREDS, pause=0, session=FakePresto(**kw))


# --------------------------------------------------------------------------- parsing units

def test_parse_money_cents():
    assert parse_money(1575) == Decimal("15.75")
    assert parse_money(-531) == Decimal("-5.31")
    assert parse_money(None) == Decimal(0)


def test_parse_time_is_toronto():
    dt = parse_time("2026-05-25T12:14:20.604Z")
    assert dt.tzinfo is not None
    assert dt.strftime("%Y-%m-%d %H:%M") == "2026-05-25 08:14"  # UTC-4 in May


def test_classification():
    assert is_trip({"discriminator": "Usage"})
    assert is_trip({"tapType": "TAP_ON"})
    assert not is_trip({"discriminator": "ActionItem"})
    assert is_system({"discriminator": "ActionItem"})
    assert not is_system({"discriminator": "Usage"})


def test_trip_and_order_mapping():
    trip = trip_from_row({**TRANSACTIONS["11110001"][0], "_account_id": "11110001"})
    assert trip.operator == "GO Transit" and trip.start_location == "Union Station Rail"
    assert trip.fare == Decimal("6.48") and trip.end_time is None
    order = order_from_row({"discriminator": "Reload", "transactionTime": "2026-05-01T09:00:00Z",
                            "uuid": "o1", "amount": 2000, "status": "ACCEPTED", "_account_id": "11110001"})
    assert order.amount == Decimal("20.00") and order.order_type == "Reload"


def test_card_mapping_balance_and_status():
    by_inst = {m["mediaInfo"]["mediaInstanceId"]: m for m in MEDIA_DETAIL["mediaList"]}
    card = card_from_instance(INSTANCES[0], by_inst["inst-1"])
    assert card.account_id == "11110001"              # dpan, used by getTransactions
    assert card.card_number == "31240011110001001"
    assert card.nickname == "Commute" and card.status == "ACTIVE"
    assert card.cash_balance == Decimal("15.75")


def test_pay_as_you_go_has_no_passes():
    # commutePlans carries placeholder slots (empty dict/list) plus, occasionally, a named plan.
    detail = {"mediaInfo": {"mediaStatus": "ACTIVE", "mediaInstanceId": "inst-1"},
              "products": {"funds": 500, "commutePlans": {"1": {}, "10": {}, "11": []}, "subscriptions": []}}
    assert card_from_instance(INSTANCES[0], detail).passes == []
    detail["products"]["commutePlans"]["2"] = {"productName": "GO Monthly"}
    assert card_from_instance(INSTANCES[0], detail).passes == ["GO Monthly"]


def test_jwt_claims_roundtrip():
    assert jwt_claims(_jwt({"ticketingAccountId": ACCOUNT_ID}))["ticketingAccountId"] == ACCOUNT_ID


# --------------------------------------------------------------------------- client flow

def test_login_cards_trips_orders():
    with client() as c:
        cards = c.cards()
        assert [x.account_id for x in cards] == ["11110001", "11110002"]
        assert cards[0].cash_balance == Decimal("15.75") and cards[0].status == "ACTIVE"

        trips = c.trips(date(2026, 1, 1), date(2026, 10, 14))
        assert len(trips) == 2                        # two taps; the ActionItem is dropped
        assert trips[0].start_time <= trips[1].start_time
        assert {t.operator for t in trips} == {"GO Transit"}

        orders = c.orders(date(2026, 1, 1), date(2026, 10, 14))
        assert orders == []                           # no loads/purchases; system events excluded


def test_wrong_password_raises_and_is_not_retried():
    sess = FakePresto()
    c = PrestoClient(Credentials("rider@example.com", "wrong"), pause=0, session=sess)
    with pytest.raises(LoginFailed):
        c.login()
    assert sum(1 for m, u in sess.calls if u.endswith("/SelfAsserted")) == 1


def test_missing_auth_code_reports_mfa_hint():
    c = client(with_code=False)
    with pytest.raises(TransitError, match="MFA|captcha|authorization code"):
        c.login()


def test_read_only_allowlist_blocks_other_endpoints():
    c = client()
    c.login()
    with pytest.raises(UnsafeRequest):
        c._ciam("media/purchase", {})


# --------------------------------------------------------------------------- tap / transfer detail

def test_tap_direction_transfer_credit_and_extra_preserved():
    row = {"_account_id": "11110001", "discriminator": "Usage", "transactionTime": "2026-05-25T12:14:20.604Z",
           "uuid": "tap-x", "serviceProviderName": "GO Transit", "locationName": "Union Station Rail",
           "transactionType": 14, "tapType": "TAP_ON", "tapMode": "TAP_ON_TAP_OFF", "usageType": "DIRECT_TAP",
           "routeTypeName": "Regular", "tripPrice": 648, "deductedFare": 456, "newEpurseBalance": -288,
           "discountItems": [{"discountType": "TRANSFER", "discountAmount": 192},
                             {"discountType": "LOYALTY", "discountAmount": 0}]}
    t = trip_from_row(row)
    assert t.tap == "on"
    assert t.transfer is True
    assert t.transfer_credit == Decimal("1.92")      # only the TRANSFER discount, not LOYALTY
    assert t.fare == Decimal("4.56")
    extra = t.to_dict()["extra"]                      # nothing thrown away: lives in JSON output
    assert extra["balance_after"] == -2.88 and extra["route_type"] == "Regular"
    assert extra["tap_mode"] == "TAP_ON_TAP_OFF"
    assert {d["type"] for d in extra["discounts"]} == {"TRANSFER", "LOYALTY"}


def test_loyalty_only_tap_is_not_a_transfer():
    row = {"_account_id": "1", "discriminator": "Usage", "transactionTime": "2026-05-25T10:00:00Z", "uuid": "y",
           "serviceProviderName": "TTC", "locationName": "X", "tapType": "TAP_OFF", "deductedFare": 330,
           "discountItems": [{"discountType": "LOYALTY", "discountAmount": 70}]}
    t = trip_from_row(row)
    assert t.tap == "off" and t.transfer is False and t.transfer_credit is None


def test_report_surfaces_taps_and_transfers():
    from transitlog import build_report, render_report
    with client() as c:
        trips = c.trips(date(2026, 1, 1), date(2026, 10, 14))
    rep = build_report(trips, [], date(2026, 1, 1), date(2026, 10, 14))
    assert rep["taps"] == {"on": 1, "off": 1}
    assert "Taps: 1 on, 1 off" in render_report(rep)
