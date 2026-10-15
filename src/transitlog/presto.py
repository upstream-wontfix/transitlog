# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Amol Venkataraman
"""Read-only client for the PRESTO (Metrolinx, Ontario) account website.

prestocard.ca is a single-page app with no public API. Unlike clippercard.com
(a server-rendered form login), PRESTO signs in through Azure AD B2C and then
reads the account from Metrolinx's CIAM backend as JSON. This client reproduces
what the browser does.

1. Azure AD B2C, custom policy ``b2c_1a_registered_user_signin_cws_v1``, OAuth2
   authorization-code flow with PKCE:
     a. GET ``/authorize``; B2C returns an HTML sign-in page that embeds a
        ``SETTINGS = { ... }`` JSON blob with a CSRF token and a transaction id,
        and sets ``x-ms-cpim-*`` cookies.
     b. POST the username and password to the policy's ``SelfAsserted`` endpoint
        (the CSRF token goes in the ``X-CSRF-TOKEN`` header). This is the exact
        analogue of Clipper's single form POST, just one of several steps.
     c. GET ``.../confirmed``; B2C answers with a redirect to ``.../signingin``
        carrying the authorization ``code``.
     d. Exchange the code (with the PKCE verifier) at ``/token`` for a token.
   The access token carries the account id and card list as claims. A wrong
   password is reported once and never retried, so this tool can't lock the
   account by hammering the login (same policy as the Clipper client).

2. The CIAM account API at ``https://api-prod.prestoprod.net/atscustomer-ciam/v4``.
   Every call needs an Azure APIM subscription key (a value the site ships in
   its own front-end config -- not a user secret), the bearer token and a
   ``ChannelType`` header. Only three read endpoints are used:
     - ``account/get``          -> the account's cards (media)
     - ``media/get``            -> each card's balance, status, passes
     - ``media/getTransactions``-> each card's taps and loads

Metrolinx's backend can also reach purchase, reload and card-management
operations. ``_ciam`` only allows the three paths in ``CIAM_ALLOWED``, so none
of those can be reached from this client.

PRESTO has no per-order PDF receipts or monthly statement PDFs like Clipper, so
``receipt``/``statements`` are not implemented; the CLI reports that plainly.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
import re
import secrets
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import requests

from . import __version__
from .config import Credentials
from .errors import LoginFailed, TransitError, UnsafeRequest, explain_network
from .models import Card, Order, Trip

SITE = "prestocard.ca"
SITE_URL = "https://www.prestocard.ca"
CIAM_BASE = "https://api-prod.prestoprod.net/atscustomer-ciam/v4"
CHANNEL_TYPE = "SELF_CUSTOMER_WEBSITE"
LANGUAGE = "EN_CA"
TZ = ZoneInfo("America/Toronto")
USER_AGENT = f"transitlog/{__version__} (+https://github.com/upstream-wontfix/transitlog)"
REQUEST_GAP = 0.5  # seconds between requests; the site sits behind Azure bot protection

# The APIM subscription key the PRESTO front end ships for the CIAM API. It's a
# public front-end config value (every browser sends it), not a user secret.
# Override with $TRANSITLOG_PRESTO_SUBSCRIPTION_KEY if Metrolinx rotates it.
CIAM_SUBSCRIPTION_KEY = "4464e49440d644d2962d6796ec93571a"

# Read-only CIAM paths. Anything else (media/purchase, account changes, ...) is refused.
CIAM_ALLOWED: set[str] = {"account/get", "media/get", "media/getTransactions"}

# The transaction-type codes the site requests to pull the full history (taps + loads).
TRANSACTION_TYPES = [1, 2, 3, 7, 8, 10, 11, 12, 13, 14, 15, 16, 17, 19, 20, 21, 22, 23,
                     25, 26, 27, 28, 30, 31]
PAGE_SIZE = 100
MAX_PAGES = 200  # safety stop for the continuation-token loop

# Azure AD B2C: values the site's own authorize request uses (public; they travel
# in every browser's sign-in URL).
B2C_HOST = "https://prestocardprodb2c.b2clogin.com"
B2C_TENANT = "b2cprestocard.net"
B2C_POLICY = "b2c_1a_registered_user_signin_cws_v1"
B2C_CLIENT_ID = "dbf68e4d-63b9-48a2-8609-6a00fdf21a4f"
B2C_REDIRECT_URI = "https://prestocard.ca/en/signingin"
B2C_SCOPE = "openid profile offline_access"
B2C_AUTHORITY = f"{B2C_HOST}/{B2C_TENANT}/{B2C_POLICY}"

SETTINGS_RE = re.compile(r"SETTINGS\s*=\s*(\{.*?\})\s*;", re.DOTALL)


# --------------------------------------------------------------------------- parsing helpers

def parse_money(cents: Any) -> Decimal:
    """CIAM reports money as integer cents (e.g. -531 == -$5.31)."""
    if cents is None or cents == "":
        return Decimal(0)
    try:
        return (Decimal(int(cents)) / 100).quantize(Decimal("0.01"))
    except (ValueError, TypeError, InvalidOperation):
        raise TransitError(f"unexpected amount {cents!r} in PRESTO data") from None


def parse_time(value: str | None) -> datetime | None:
    """Parse CIAM's UTC ISO timestamps (``2026-05-25T12:14:20.604Z``) as Toronto time."""
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise TransitError(f"unexpected date {value!r} in PRESTO data") from None
    return dt.replace(tzinfo=timezone.utc).astimezone(TZ) if dt.tzinfo is None else dt.astimezone(TZ)


def _iso_utc(d: date, end: bool = False) -> str:
    """A Toronto calendar day as a UTC ISO instant for the CIAM time window."""
    local = datetime(d.year, d.month, d.day, tzinfo=TZ)
    if end:
        local += timedelta(days=1)
    return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _str(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _first(d: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return default


def jwt_claims(token: str) -> dict[str, Any]:
    """Decode a JWT payload without verifying (we only read our own token's claims)."""
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part))
    except (IndexError, ValueError, binascii.Error) as e:
        raise TransitError("couldn't read the PRESTO access token", raw=e) from e


def _media_list_from_claim(claims: dict[str, Any]) -> list[dict[str, Any]]:
    """The token's ``medias`` claim: a list of JSON strings, each {mediaType, mediaId, mediaInstanceId}."""
    raw = claims.get("medias")
    if isinstance(raw, str):
        raw = json.loads(raw)
    return [json.loads(x) if isinstance(x, str) else x for x in (raw or [])]


# --------------------------------------------------------------------------- client

class PrestoClient:
    """Log in to prestocard.ca via Azure AD B2C and read cards and transactions.

    Use as a context manager; it logs in on entry and clears the token on exit.
    The public interface mirrors ``ClipperClient`` so the CLI and report code are
    provider-agnostic.
    """

    provider = "presto"

    def __init__(self, credentials: Credentials, timeout: int = 30, logger: logging.Logger | None = None,
                 pause: float = REQUEST_GAP, session: requests.Session | None = None,
                 subscription_key: str | None = None):
        self.credentials = credentials
        self.timeout = timeout
        self.log = logger or logging.getLogger("transitlog.presto")
        self.pause = pause
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self._subscription_key = (subscription_key or os.environ.get("TRANSITLOG_PRESTO_SUBSCRIPTION_KEY")
                                  or CIAM_SUBSCRIPTION_KEY)
        self._token: str | None = None
        self._account_id: str | None = None
        self._instances: list[dict[str, Any]] | None = None  # account/get media instances
        self._last_request = 0.0
        self.warnings: list[str] = []

    def __enter__(self) -> PrestoClient:
        self.login()
        return self

    def __exit__(self, *exc: object) -> None:
        self.logout()

    # ----------------------------------------------------------------- transport

    def _send(self, method: str, url: str, *, allow_redirects: bool = True, **kwargs: Any) -> requests.Response:
        wait = self._last_request + self.pause - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        try:
            r = self.session.request(method, url, timeout=self.timeout, allow_redirects=allow_redirects, **kwargs)
        except requests.RequestException as e:
            raise TransitError(explain_network(e, SITE), raw=e) from e
        finally:
            self._last_request = time.monotonic()
        self.log.debug("%s %s -> %s %s (%d bytes)", method, urlparse(url).path, r.status_code,
                       r.headers.get("content-type"), len(r.content))
        return r

    def _ciam(self, path: str, body: dict[str, Any]) -> Any:
        """POST one allow-listed CIAM endpoint with the bearer token and subscription key."""
        if path not in CIAM_ALLOWED:
            raise UnsafeRequest(f"refusing PRESTO endpoint {path!r}: not on the read-only allowlist")
        if not self._token:
            raise TransitError("not logged in")
        r = self._send("POST", f"{CIAM_BASE}/{path}", data=json.dumps(body), headers={
            "Ocp-Apim-Subscription-Key": self._subscription_key,
            "Authorization": f"Bearer {self._token}",
            "ChannelType": CHANNEL_TYPE,
            "Content-Type": "application/json", "Accept": "application/json",
            "Origin": SITE_URL, "Referer": SITE_URL + "/",
        })
        if r.status_code == 401:
            raise TransitError("the PRESTO session expired or was rejected; run the command again", raw=r.text[:300])
        if "SubscriptionKey" in r.text[:60]:
            raise TransitError(
                "PRESTO rejected the API subscription key; Metrolinx may have rotated it. Set "
                "$TRANSITLOG_PRESTO_SUBSCRIPTION_KEY to the 'Ocp-Apim-Subscription-Key' value your browser sends.",
                raw=r.text[:200])
        if r.status_code >= 400:
            raise TransitError(f"{SITE} returned HTTP {r.status_code} for {path}", raw=r.text[:500])
        try:
            return r.json()
        except ValueError:
            raise TransitError(f"PRESTO's {path} response was not JSON; the site may have changed",
                               raw=r.text[:500]) from None

    # ----------------------------------------------------------------- session

    @staticmethod
    def _pkce() -> tuple[str, str]:
        verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
        digest = hashlib.sha256(verifier.encode()).digest()
        challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        return verifier, challenge

    def login(self) -> None:
        verifier, challenge = self._pkce()
        state, nonce = secrets.token_urlsafe(16), secrets.token_urlsafe(16)
        authorize = self._send("GET", f"{B2C_AUTHORITY}/oauth2/v2.0/authorize", params={
            "client_id": B2C_CLIENT_ID, "scope": B2C_SCOPE, "redirect_uri": B2C_REDIRECT_URI,
            "response_type": "code", "response_mode": "query", "prompt": "login",
            "code_challenge": challenge, "code_challenge_method": "S256", "nonce": nonce, "state": state,
        })
        settings = self._b2c_settings(authorize.text)
        csrf, trans_id = settings["csrf"], settings["transId"]

        # Submit the credentials (one attempt; never retried, to avoid lockout).
        self_asserted = self._send("POST", f"{B2C_AUTHORITY}/SelfAsserted",
                                    params={"tx": trans_id, "p": B2C_POLICY}, allow_redirects=False,
                                    headers={"X-CSRF-TOKEN": csrf, "X-Requested-With": "XMLHttpRequest"},
                                    data={"request_type": "RESPONSE",
                                          "signInName": self.credentials.username,
                                          "password": self.credentials.password})
        self._check_credentials(self_asserted)

        confirmed = self._send("GET", f"{B2C_AUTHORITY}/api/CombinedSigninAndSignup/confirmed",
                               params={"rememberMe": "false", "csrf_token": csrf, "tx": trans_id, "p": B2C_POLICY},
                               allow_redirects=False)
        code = self._auth_code(confirmed)
        token = self._send("POST", f"{B2C_AUTHORITY}/oauth2/v2.0/token", data={
            "grant_type": "authorization_code", "client_id": B2C_CLIENT_ID, "scope": B2C_SCOPE,
            "code": code, "redirect_uri": B2C_REDIRECT_URI, "code_verifier": verifier,
        }).json()
        self._token = token.get("access_token") or token.get("id_token")
        if not self._token:
            raise LoginFailed(f"PRESTO returned no access token for {self.credentials.username}")
        claims = jwt_claims(self._token)
        self._account_id = _str(_first(claims, "ticketingAccountId")) or _str(
            (claims.get("accountDetails") or {}).get("ticketingAccountId"))
        if not self._account_id:
            raise TransitError("the PRESTO token had no account id; the sign-in policy may have changed",
                               raw=sorted(claims))

    @staticmethod
    def _b2c_settings(html: str) -> dict[str, Any]:
        m = SETTINGS_RE.search(html)
        if not m:
            raise TransitError("couldn't find the PRESTO sign-in form (B2C SETTINGS); the site may have changed")
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError as e:
            raise TransitError("couldn't parse the PRESTO sign-in form data", raw=e) from e
        if not data.get("csrf") or not data.get("transId"):
            raise TransitError("the PRESTO sign-in form was missing its CSRF/transaction token", raw=data)
        return data

    def _check_credentials(self, r: requests.Response) -> None:
        """B2C answers SelfAsserted with JSON: status '200' on success, else an error message."""
        try:
            body = r.json()
        except ValueError:
            body = {}
        if str(body.get("status", r.status_code)) == "200":
            return
        message = _str(body.get("message")) or "check the username and password file"
        raise LoginFailed(f"PRESTO rejected the login for {self.credentials.username}: {message}. "
                          "Repeated failures can lock the account, so this tool never retries on its own.")

    @staticmethod
    def _auth_code(r: requests.Response) -> str:
        parts = urlparse(r.headers.get("Location", ""))
        params = {**parse_qs(parts.query), **parse_qs(parts.fragment)}  # response_mode may use either
        code = params.get("code", [None])[0]
        if not code:
            raise TransitError("PRESTO didn't return an authorization code after sign-in; the account may need "
                               "a step this tool can't do (e.g. MFA or a captcha)",
                               raw=r.headers.get("Location") or r.text[:300])
        return code

    def logout(self) -> None:
        self._token = None  # no documented logout endpoint; dropping the token is enough for a CLI run

    # ----------------------------------------------------------------- data

    def _account_instances(self) -> list[dict[str, Any]]:
        """The account's cards (media) from account/get, each with dpan/mediaInstanceId/versionNbr."""
        if self._instances is None:
            data = self._ciam("account/get", {"accountId": self._account_id})
            account = data.get("account") if isinstance(data, dict) else None
            self._instances = (account or {}).get("mediaInstances") or []
        return self._instances

    def cards(self) -> list[Card]:
        instances = self._account_instances()
        if not instances:
            return []
        inputs = [{"mediaInstanceId": i.get("mediaInstanceId"), "mediaType": i.get("mediaType"),
                   "versionNbr": i.get("versionNbr")} for i in instances]
        detail = self._ciam("media/get", {"language": LANGUAGE, "mediaInputs": inputs})
        by_instance = {}
        for item in (detail.get("mediaList") if isinstance(detail, dict) else None) or []:
            key = (item.get("mediaInfo") or {}).get("mediaInstanceId")
            if key:
                by_instance[key] = item
        return [card_from_instance(i, by_instance.get(i.get("mediaInstanceId"))) for i in instances]

    def select_cards(self, selector: str | None) -> list[Card]:
        """All cards, or the one whose account id (dpan), card number or last four digits match."""
        cards = self.cards()
        if not selector:
            return cards
        found = [c for c in cards if selector in {c.account_id, c.card_number}
                 or (len(selector) == 4 and (c.card_number or "").endswith(selector))]
        if not found:
            known = ", ".join(c.label for c in cards) or "none"
            raise TransitError(f"no card matches {selector!r} (cards on this account: {known})")
        return found

    def _raw_transactions(self, since: date, until: date, card: str | None) -> list[dict[str, Any]]:
        if since > until:
            raise TransitError(f"start date {since} is after end date {until}")
        by_dpan = {i.get("dpan"): i for i in self._account_instances()}
        rows: list[dict[str, Any]] = []
        for c in self.select_cards(card):
            inst = by_dpan.get(c.account_id)
            if inst is None:
                continue
            token: str | None = None
            for _ in range(MAX_PAGES):
                body = {
                    "transactionType": TRANSACTION_TYPES,
                    "mediaInput": {"dpan": inst.get("dpan"), "mediaType": inst.get("mediaType")},
                    "startTime": _iso_utc(since), "endTime": _iso_utc(until, end=True),
                    "recordsReturned": str(PAGE_SIZE), "sortDesc": True, "language": LANGUAGE,
                }
                if token:
                    body["continuationToken"] = token
                data = self._ciam("media/getTransactions", body)
                batch = (data.get("transactions") if isinstance(data, dict) else None) or []
                for tr in batch:
                    tr["_account_id"] = c.account_id
                rows += batch
                token = data.get("continuationToken") if isinstance(data, dict) else None
                if not token or not batch:
                    break
        return rows

    def trips(self, since: date, until: date, card: str | None = None) -> list[Trip]:
        """Every tap between `since` and `until` (inclusive, Toronto time), oldest first."""
        trips = [trip_from_row(r) for r in self._raw_transactions(since, until, card) if is_trip(r)]
        trips = [t for t in trips if since <= t.start_time.date() <= until]
        return sorted(trips, key=lambda t: (t.start_time, t.trip_id))

    def orders(self, since: date, until: date, card: str | None = None) -> list[Order]:
        """Every load/purchase in the range (system events and taps excluded), oldest first."""
        orders = [order_from_row(r) for r in self._raw_transactions(since, until, card)
                  if not is_trip(r) and not is_system(r)]
        orders = [o for o in orders if since <= o.time.date() <= until]
        return sorted(orders, key=lambda o: (o.time, o.order_id))

    def receipt(self, order_id: str) -> bytes:
        raise TransitError("PRESTO doesn't offer per-order PDF receipts; download the CSV/JSON instead")

    def statements(self, since: date, until: date, card: str | None = None):
        raise TransitError("PRESTO doesn't offer monthly statement PDFs; use the report or CSV/JSON export")


# --------------------------------------------------------------------------- response mapping

def card_from_instance(inst: dict[str, Any], detail: dict[str, Any] | None) -> Card:
    """Build a Card from an account/get instance and its media/get detail (if present)."""
    info = (detail or {}).get("mediaInfo") or {}
    products = (detail or {}).get("products") or {}
    profile = (detail or {}).get("mediaProfile") or {}
    commute = products.get("commutePlans") or {}
    subs = products.get("subscriptions") or []
    # commutePlans carries one entry per plan slot, most of them empty placeholders
    # on a pay-as-you-go card. Only surface a slot that actually names a product.
    plans = commute.values() if isinstance(commute, dict) else commute if isinstance(commute, list) else []
    passes = [name for plan in plans if isinstance(plan, dict) and plan
              for name in [_str(_first(plan, "name", "productName", "planName", "productType", "type"))] if name]
    funds = products.get("funds")
    return Card(
        provider="presto",
        account_id=str(inst.get("dpan") or ""),            # dpan: the id getTransactions needs
        card_number=_str(info.get("visibleCardNumber") or inst.get("mediaReferenceId")),
        nickname=_str(inst.get("customName")),
        status=_str(info.get("mediaStatus")),
        rider_class=_str(_concession_name(profile)),
        cash_balance=parse_money(funds) if funds is not None else None,
        passes=[p for p in passes if p],
        autoload=bool(subs),
        activated=_str(info.get("physicalCardConversionTimestamp")),
        expires=_str(_concession_expiry(profile)),
    )


def _concession_name(profile: dict[str, Any]) -> str | None:
    conc = profile.get("concessions") or {}
    vals = list(conc.values()) if isinstance(conc, dict) else conc
    if vals:
        first = vals[0]
        return _str(_first(first, "name", "description")) or (f"Concession {first.get('id')}"
                                                               if first.get("id") is not None else None)
    return None


def _concession_expiry(profile: dict[str, Any]) -> str | None:
    conc = profile.get("concessions") or {}
    vals = list(conc.values()) if isinstance(conc, dict) else conc
    return _str(vals[0].get("expirationDate")) if vals else None


# getTransactions mixes three kinds of row by `discriminator`: "Usage" (a tap),
# a load/purchase (money added), and "ActionItem" (a system event such as a card
# migration -- no money and not transit use, so it is dropped from both views).
SYSTEM_DISCRIMINATORS = {"actionitem"}


def is_system(row: dict[str, Any]) -> bool:
    return str(row.get("discriminator", "")).lower() in SYSTEM_DISCRIMINATORS


def is_trip(row: dict[str, Any]) -> bool:
    """A tap/usage event (vs. a load, an adjustment or a system event)."""
    if str(row.get("discriminator", "")).lower() == "usage":
        return True
    return bool(row.get("tapType") or row.get("usageType"))


TAP_DIRECTION = {"TAP_ON": "on", "TAP_OFF": "off"}
# Discount types that mean a transfer / inter-agency co-fare (vs. LOYALTY and others).
TRANSFER_DISCOUNTS = {"TRANSFER", "CO_FARE", "COFARE"}


def _discounts(row: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for d in row.get("discountItems") or []:
        if isinstance(d, dict):
            out.append({"type": _str(d.get("discountType")),
                        "amount": float(parse_money(d.get("discountAmount")))})
    return out


def trip_from_row(row: dict[str, Any]) -> Trip:
    start = parse_time(row.get("transactionTime"))
    if start is None:
        raise TransitError(f"PRESTO tap {row.get('uuid')} has no time", raw=row)
    fare = _first(row, "deductedFare", "tripPrice", default=0)
    discounts = _discounts(row)
    transfer_items = [d for d in row.get("discountItems") or []
                      if isinstance(d, dict) and str(d.get("discountType", "")).upper() in TRANSFER_DISCOUNTS]
    transfer_credit = sum((parse_money(d.get("discountAmount")) for d in transfer_items), Decimal(0))
    # Everything the source reported that has no common column -- kept so nothing is lost.
    extra = {k: v for k, v in {
        "tap_mode": _str(row.get("tapMode")), "usage_type": _str(row.get("usageType")),
        "route_type": _str(row.get("routeTypeName")), "transaction_type": row.get("transactionType"),
        "trip_price": float(parse_money(row.get("tripPrice"))) if row.get("tripPrice") is not None else None,
        "balance_after": float(parse_money(row.get("newEpurseBalance"))) if row.get("newEpurseBalance") is not None
        else None,
        "status": _str(row.get("status")), "discounts": discounts or None,
    }.items() if v is not None}
    return Trip(
        provider="presto",
        trip_id=str(_first(row, "uuid", "seqNbr", default="")),
        account_id=str(row.get("_account_id", "")),
        operator=str(_first(row, "serviceProviderName", default="")),
        start_time=start,
        start_location=_str(_first(row, "locationName", "stopName")),
        end_time=None,       # PRESTO reports each tap separately (tap-on and tap-off are distinct rows)
        end_location=None,
        fare=parse_money(fare),
        product=None,        # paid from the card's e-purse; a zero fare reads as a free/pass tap
        tap=TAP_DIRECTION.get(str(row.get("tapType", "")).upper()),
        transfer=bool(transfer_items),
        transfer_credit=transfer_credit or None,
        extra=extra,
    )


def order_from_row(row: dict[str, Any]) -> Order:
    when = parse_time(row.get("transactionTime"))
    if when is None:
        raise TransitError(f"PRESTO transaction {row.get('uuid')} has no time", raw=row)
    kind = _str(row.get("discriminator")) or "Transaction"
    amount = _first(row, "amount", "loadAmount", "creditAmount", "tripPrice", "deductedFare", default=0)
    return Order(
        provider="presto",
        order_id=str(_first(row, "uuid", "seqNbr", default="")),
        account_id=str(row.get("_account_id", "")),
        time=when,
        amount=parse_money(amount),
        status=_str(row.get("status")),
        order_type=kind,
        payment=_str(_first(row, "paymentType", "fundingSource", "tenderType")),
        payment_ref=_str(_first(row, "referenceNumber", "authCode")),
        products=[p for p in [_str(_first(row, "serviceProviderName")), kind] if p][:1] or [kind],
    )
