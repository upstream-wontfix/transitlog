# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Amol Venkataraman
"""Provider-neutral records. Money is Decimal internally; `to_dict()` gives JSON-friendly values."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any


def _plain(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


class _Record:
    def to_dict(self) -> dict[str, Any]:
        return {k: _plain(v) for k, v in asdict(self).items()}


@dataclass
class Card(_Record):
    provider: str
    account_id: str          # the transit account the history is filed under
    card_number: str | None  # number printed on the card
    nickname: str | None
    status: str | None
    rider_class: str | None
    cash_balance: Decimal | None
    passes: list[str] = field(default_factory=list)
    autoload: bool = False
    activated: str | None = None
    expires: str | None = None

    @property
    def label(self) -> str:
        last4 = (self.card_number or self.account_id)[-4:]
        return f"{self.nickname} ({last4})" if self.nickname else last4


@dataclass
class Trip(_Record):
    provider: str
    trip_id: str
    account_id: str
    operator: str
    start_time: datetime
    start_location: str | None
    end_time: datetime | None
    end_location: str | None
    fare: Decimal
    product: str | None  # what paid for it: a pass name, "Cash Value", or None (e.g. a free transfer)
    # Cross-provider tap detail (optional; populated where the source reports it):
    tap: str | None = None              # "on" / "off" on tap-each-end systems (PRESTO); None if not reported
    transfer: bool = False              # this ride used a transfer (free or discounted)
    transfer_credit: Decimal | None = None  # the transfer/co-fare credit applied, if the source gives an amount
    # Anything else the source reported but that has no common column, kept for JSON exports
    # so no data is lost (e.g. PRESTO's tap_mode, route_type, balance_after, discounts).
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Order(_Record):
    provider: str
    order_id: str
    account_id: str
    time: datetime
    amount: Decimal
    status: str | None
    order_type: str | None
    payment: str | None
    payment_ref: str | None
    products: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)  # source-specific fields, kept for JSON exports
