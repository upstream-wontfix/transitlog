# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Amol Venkataraman
"""Usage summary built only from downloaded trips and orders (no fare tables or guesses)."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal
from typing import Any

from .models import Card, Order, Trip

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
CASH_VALUE = "Cash Value"


def paid_with(trip: Trip) -> str:
    """'pass', 'cash value' or 'free' (no charge and no pass, e.g. a transfer)."""
    if trip.product and trip.product != CASH_VALUE:
        return "pass"
    if trip.fare or trip.product == CASH_VALUE:
        return "cash value"
    return "free"


def build_report(trips: list[Trip], orders: list[Order], since: date, until: date,
                 cards: list[Card] | None = None) -> dict[str, Any]:
    days = {t.start_time.date() for t in trips}
    report: dict[str, Any] = {
        "period": {"since": since.isoformat(), "until": until.isoformat()},
        "trips": {
            "total": len(trips),
            "active_days": len(days),
            "per_active_day": round(len(trips) / len(days), 2) if days else 0,
            "first": trips[0].start_time.isoformat() if trips else None,
            "last": trips[-1].start_time.isoformat() if trips else None,
        },
        "paid_with": dict(Counter(paid_with(t) for t in trips).most_common()),
    }
    transfers = [t for t in trips if getattr(t, "transfer", False)]
    report["transfers"] = {
        "count": len(transfers),
        "credit": sum((t.transfer_credit for t in transfers if t.transfer_credit), Decimal(0)),
    }
    taps = Counter(t.tap for t in trips if getattr(t, "tap", None))
    if taps:  # only tap-each-end sources (e.g. PRESTO) report a direction
        report["taps"] = {"on": taps.get("on", 0), "off": taps.get("off", 0)}
    if cards is not None:
        report["cards"] = [{"card": c.label, "status": c.status, "cash_balance": c.cash_balance,
                            "passes": c.passes} for c in cards]

    by_op: dict[str, list[Trip]] = defaultdict(list)
    for t in trips:
        by_op[t.operator].append(t)
    report["by_operator"] = [
        {"operator": op, "trips": len(ts), "fares": sum((t.fare for t in ts), Decimal(0))}
        for op, ts in sorted(by_op.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    ]

    months: dict[str, dict[str, Any]] = defaultdict(lambda: {"trips": 0, "fares": Decimal(0), "spent": Decimal(0)})
    for t in trips:
        m = months[t.start_time.strftime("%Y-%m")]
        m["trips"] += 1
        m["fares"] += t.fare
    for o in orders:
        months[o.time.strftime("%Y-%m")]["spent"] += o.amount
    report["by_month"] = [{"month": k, **v} for k, v in sorted(months.items())]

    weekday = Counter(t.start_time.weekday() for t in trips)
    report["by_weekday"] = [{"weekday": WEEKDAYS[i], "trips": weekday.get(i, 0)} for i in range(7)]
    hours = Counter(t.start_time.hour for t in trips)
    report["busiest_hour"] = f"{hours.most_common(1)[0][0]:02d}:00" if hours else None

    stations = Counter(s for t in trips for s in (t.start_location, t.end_location) if s)
    report["top_stations"] = [{"station": s, "taps": n} for s, n in stations.most_common(10)]
    journeys = Counter((t.start_location, t.end_location) for t in trips if t.start_location and t.end_location)
    report["top_journeys"] = [{"from": a, "to": b, "trips": n} for (a, b), n in journeys.most_common(5)]

    by_product: dict[str, dict[str, Any]] = defaultdict(lambda: {"orders": 0, "amount": Decimal(0)})
    for o in orders:
        p = by_product[" + ".join(o.products) or "(unknown)"]
        p["orders"] += 1
        p["amount"] += o.amount
    report["spending"] = {
        "orders_total": sum((o.amount for o in orders), Decimal(0)),
        "fares_charged": sum((t.fare for t in trips), Decimal(0)),
        "by_product": [{"product": k, **v} for k, v in sorted(by_product.items(), key=lambda kv: -kv[1]["amount"])],
    }

    pass_rides = Counter(t.product for t in trips if paid_with(t) == "pass")
    pass_cost: dict[str, list[Decimal]] = defaultdict(list)
    for o in orders:
        if len(o.products) == 1 and o.products[0] != CASH_VALUE:  # price is known only for single-product orders
            pass_cost[o.products[0]].append(o.amount)
    report["passes"] = []
    for name in sorted(set(pass_rides) | set(pass_cost)):
        cost = sum(pass_cost.get(name, []), Decimal(0))
        rides = pass_rides.get(name, 0)
        report["passes"].append({
            "pass": name, "purchases": len(pass_cost.get(name, [])), "cost": cost, "rides": rides,
            "cost_per_ride": (cost / rides).quantize(Decimal("0.01")) if rides and cost else None,
        })
    return report


def _money(value: Any) -> str:
    return "-" if value is None else f"${Decimal(value):,.2f}"


def _table(rows: list[list[str]], right: frozenset[int] = frozenset()) -> list[str]:
    """Indented plain-text table; columns whose index is in `right` are right-aligned."""
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]

    def line(r: list[str]) -> str:
        cells = (c.rjust(w) if i in right else c.ljust(w) for i, (c, w) in enumerate(zip(r, widths, strict=True)))
        return ("  " + "  ".join(cells)).rstrip()
    return [line(r) for r in rows]


def render_report(report: dict[str, Any]) -> str:
    t = report["trips"]
    out = [f"Transit usage {report['period']['since']} to {report['period']['until']}", ""]
    for c in report.get("cards", []):
        passes = f", passes: {', '.join(c['passes'])}" if c["passes"] else ""
        out.append(f"Card {c['card']}: {c['status']}, cash value {_money(c['cash_balance'])}{passes}")
    if report.get("cards"):
        out.append("")
    if not t["total"]:
        out.append("No trips in this period.")
    else:
        paid = ", ".join(f"{n} {k}" for k, n in report["paid_with"].items())
        out += [f"Trips: {t['total']} on {t['active_days']} days ({t['per_active_day']} per active day)",
                f"First/last: {t['first'][:16].replace('T', ' ')} / {t['last'][:16].replace('T', ' ')}",
                f"Paid with: {paid}"]
        if report.get("taps"):
            out.append(f"Taps: {report['taps']['on']} on, {report['taps']['off']} off")
        tr = report.get("transfers") or {}
        if tr.get("count"):
            credit = f" (transfer credit {_money(tr['credit'])})" if tr.get("credit") else ""
            out.append(f"Transfers: {tr['count']}{credit}")
        out += [f"Busiest hour: {report['busiest_hour']}", "",
                "By operator"]
        out += _table([["operator", "trips", "fares"]] +
                      [[r["operator"], str(r["trips"]), _money(r["fares"])] for r in report["by_operator"]], {1, 2})
    out += ["", "By month"]
    out += _table([["month", "trips", "fares", "spent"]] +
                  [[r["month"], str(r["trips"]), _money(r["fares"]), _money(r["spent"])] for r in report["by_month"]],
                  {1, 2, 3})
    if t["total"]:
        out += ["", "By weekday"]
        out += _table([[r["weekday"], str(r["trips"])] for r in report["by_weekday"]], {1})
        out += ["", "Top stations/stops"]
        out += _table([[r["station"], str(r["taps"])] for r in report["top_stations"]], {1})
        if report["top_journeys"]:
            out += ["", "Top journeys (tap-in/tap-out systems)"]
            out += _table([[f"{r['from']} → {r['to']}", str(r["trips"])] for r in report["top_journeys"]], {1})
    s = report["spending"]
    out += ["", f"Spending: {_money(s['orders_total'])} in orders; {_money(s['fares_charged'])} charged as fares"]
    if s["by_product"]:
        out += _table([[r["product"], f"{r['orders']}×", _money(r["amount"])] for r in s["by_product"]], {1, 2})
    if report["passes"]:
        out += ["", "Passes"]
        out += _table([["pass", "bought", "cost", "rides", "per ride"]] +
                      [[r["pass"], str(r["purchases"]), _money(r["cost"]), str(r["rides"]), _money(r["cost_per_ride"])]
                       for r in report["passes"]], {1, 2, 3, 4})
        out.append("  (pass rides inside the period divided by passes bought inside it; a pass bought just "
                   "before the period starts isn't counted)")
    return "\n".join(out)
