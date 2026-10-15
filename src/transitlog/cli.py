# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Amol Venkataraman
"""`transitlog` command-line interface. Run `transitlog --help`."""
from __future__ import annotations

import argparse
import csv
import getpass
import json
import logging
import os
import sys
import textwrap
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from . import __version__
from .clipper import TZ as CLIPPER_TZ
from .clipper import ClipperClient
from .config import (
    CONFIG_FILE,
    ConfigError,
    default_password_file,
    resolve_credentials,
    write_password_file,
)
from .errors import TransitError
from .models import Order, Trip
from .presto import TZ as PRESTO_TZ
from .presto import PrestoClient
from .report import build_report, render_report

# Each provider: its client class, local timezone (for default "today"/range) and
# whether it offers per-order PDF receipts and monthly statement PDFs.
# "client" is the client class; _run resolves it through this module's namespace
# at call time, so tests can monkeypatch `cli.ClipperClient` / `cli.PrestoClient`.
PROVIDERS: dict[str, dict[str, Any]] = {
    "clipper": {"client": ClipperClient, "tz": CLIPPER_TZ, "receipts": True,
                "help": "Clipper card (SF Bay Area): clippercard.com account",
                "desc": "Clipper card (SF Bay Area). Needs a clippercard.com account "
                        "with the card registered to it."},
    "presto": {"client": PrestoClient, "tz": PRESTO_TZ, "receipts": False,
               "help": "PRESTO card (Ontario): prestocard.ca account",
               "desc": "PRESTO card (Ontario, Metrolinx). Needs a prestocard.ca account "
                       "with the card registered to it."},
}

GLOBAL_DEFAULTS: dict[str, Any] = {
    "username": None, "password_file": None, "timeout": 30, "config": str(CONFIG_FILE),
    "json": False, "debug": False,
}
DEFAULT_DAYS = 365
MAX_CELL = 40
TRIP_FIELDS = ["trip_id", "start_time", "operator", "start_location", "end_time", "end_location", "fare",
               "product", "tap", "transfer", "transfer_credit", "account_id"]
ORDER_FIELDS = ["order_id", "time", "amount", "products", "payment", "payment_ref", "status", "order_type",
                "account_id"]
DISCLAIMER_SHORT = ("Provided AS IS, without warranty of any kind, under the MIT License. "
                    "Written with AI assistance: review the code before running it. See DISCLAIMER.md.")


# --------------------------------------------------------------------------- output

def print_json(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


def print_table(rows: list[dict[str, Any]], columns: list[str], right: frozenset[str] = frozenset()) -> None:
    if not rows:
        print("(none)")
        return

    def cell(value: Any) -> str:
        text = "" if value is None else str(value)
        return text if len(text) <= MAX_CELL else text[: MAX_CELL - 1] + "…"

    table = [[cell(row.get(c)) for c in columns] for row in rows]
    widths = [max(len(c), *(len(r[i]) for r in table)) for i, c in enumerate(columns)]

    def line(cells: list[str]) -> str:
        return "  ".join(v.rjust(w) if c in right else v.ljust(w)
                         for v, w, c in zip(cells, widths, columns, strict=True)).rstrip()
    print(line([c.upper() for c in columns]))
    for r in table:
        print(line(r))


def flat(record: Trip | Order) -> dict[str, Any]:
    """A record as one CSV/table row: lists joined with "; ", money as fixed two decimals.

    The free-form `extra` dict is dropped here (it has no fixed column); it is kept
    only in JSON output, via `to_dict()`, so no source data is lost.
    """
    row = record.to_dict()
    row.pop("extra", None)
    for k, v in list(row.items()):
        if isinstance(v, list):
            row[k] = "; ".join(str(x) for x in v)
    for k in ("fare", "amount", "transfer_credit"):
        if k in row:
            value = getattr(record, k, None)
            row[k] = f"{value:.2f}" if value is not None else ""
    if "transfer" in row:
        row["transfer"] = "yes" if getattr(record, "transfer", False) else ""
    if row.get("tap") is None and "tap" in row:
        row["tap"] = ""
    return row


def write_records(path: Path, records: list[Trip] | list[Order], fields: list[str]) -> None:
    if path.suffix.lower() == ".json":
        path.write_text(json.dumps([r.to_dict() for r in records], indent=2) + "\n")
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(flat(r) for r in records)


def short_time(value: str | None) -> str | None:
    return value[:16].replace("T", " ") if value else value


def note(message: str) -> None:
    print(message, file=sys.stderr)


# --------------------------------------------------------------------------- dates

def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a date like 2026-05-31") from None


def resolve_range(a: argparse.Namespace) -> tuple[date, date]:
    tz = PROVIDERS[a.provider]["tz"]
    until = a.until or datetime.now(tz).date()
    since = a.since or until - timedelta(days=(a.days or DEFAULT_DAYS) - 1)
    return since, until


# --------------------------------------------------------------------------- commands

def cmd_cards(c: ClipperClient, a: argparse.Namespace) -> int:
    cards = c.cards()
    if a.json:
        print_json([x.to_dict() for x in cards])
        return 0
    rows = [{**x.to_dict(), "card": x.label,
             "cash_balance": f"${x.cash_balance:.2f}" if x.cash_balance is not None else None,
             "passes": ", ".join(x.passes) or None, "autoload": "on" if x.autoload else "off",
             "activated": (x.activated or "")[:10] or None} for x in cards]
    print_table(rows, ["card", "card_number", "account_id", "rider_class", "status", "cash_balance", "passes",
                       "autoload", "activated"], {"cash_balance"})
    return 0


def _emit_records(records: list[Trip] | list[Order], a: argparse.Namespace, fields: list[str],
                  columns: list[str], money: str) -> None:
    if a.output:
        write_records(Path(a.output), records, fields)
        note(f"saved {len(records)} record(s) to {a.output}")
    elif a.json:
        print_json([r.to_dict() for r in records])
    else:
        rows = [flat(r) for r in records]
        for row in rows:
            for k in ("start_time", "end_time", "time"):
                if k in row:
                    row[k] = short_time(row[k])
        print_table(rows, columns, {money})


def cmd_trips(c: ClipperClient, a: argparse.Namespace) -> int:
    since, until = resolve_range(a)
    trips = c.trips(since, until, a.card)
    _emit_records(trips, a, TRIP_FIELDS,
                  ["start_time", "operator", "start_location", "end_location", "fare", "product", "tap",
                   "transfer"], "fare")
    return 0


def cmd_orders(c: ClipperClient, a: argparse.Namespace) -> int:
    since, until = resolve_range(a)
    orders = c.orders(since, until, a.card)
    _emit_records(orders, a, ORDER_FIELDS, ["time", "order_id", "amount", "products", "payment"], "amount")
    return 0


def save_receipts(c: ClipperClient, orders: list[Order], directory: Path) -> tuple[int, int, int]:
    """Save each order's receipt. Returns (saved, already there, failed); failures become client warnings."""
    directory.mkdir(parents=True, exist_ok=True)
    saved = skipped = failed = 0
    for o in orders:
        path = directory / f"clipper-receipt-{o.time:%Y-%m-%d}-{o.order_id}.pdf"
        if path.exists():
            skipped += 1
            continue
        try:
            pdf = c.receipt(o.order_id)
        except TransitError as e:  # Clipper fails to generate some receipts (seen on its oldest orders)
            c.warnings.append(f"no receipt for order {o.order_id} ({o.time:%Y-%m-%d}): {e}")
            failed += 1
            continue
        path.write_bytes(pdf)
        saved += 1
    return saved, skipped, failed


def cmd_receipts(c: ClipperClient, a: argparse.Namespace) -> int:
    since, until = resolve_range(a)
    saved, skipped, failed = save_receipts(c, c.orders(since, until, a.card), Path(a.dir))
    extra = []
    if skipped:
        extra.append(f"{skipped} already there")
    if failed:
        extra.append(f"{failed} unavailable")
    note(f"saved {saved} receipt(s) to {a.dir}" + (f" ({', '.join(extra)})" if extra else ""))
    return 0


def save_statements(c: ClipperClient, since: date, until: date, card: str | None, directory: Path) -> int:
    directory.mkdir(parents=True, exist_ok=True)
    count = 0
    for card_obj, start, end, pdf in c.statements(since, until, card):
        last4 = (card_obj.card_number or card_obj.account_id)[-4:]
        (directory / f"clipper-trips-{last4}-{start}_{end}.pdf").write_bytes(pdf)
        count += 1
    return count


def cmd_statements(c: ClipperClient, a: argparse.Namespace) -> int:
    since, until = resolve_range(a)
    count = save_statements(c, since, until, a.card, Path(a.dir))
    note(f"saved {count} statement PDF(s) to {a.dir}")
    return 0


def cmd_report(c: ClipperClient, a: argparse.Namespace) -> int:
    since, until = resolve_range(a)
    cards = c.select_cards(a.card)
    report = build_report(c.trips(since, until, a.card), c.orders(since, until, a.card), since, until, cards)
    if a.json:
        print_json(report)
    else:
        print(render_report(report))
    return 0


def cmd_export(c: ClipperClient, a: argparse.Namespace) -> int:
    since, until = resolve_range(a)
    out = Path(a.dir)
    out.mkdir(parents=True, exist_ok=True)
    cards = c.select_cards(a.card)
    trips = c.trips(since, until, a.card)
    orders = c.orders(since, until, a.card)
    (out / "cards.json").write_text(json.dumps([x.to_dict() for x in cards], indent=2) + "\n")
    for name, records, fields in (("trips", trips, TRIP_FIELDS), ("orders", orders, ORDER_FIELDS)):
        write_records(out / f"{name}.csv", records, fields)
        write_records(out / f"{name}.json", records, fields)
    report = build_report(trips, orders, since, until, cards)
    (out / "report.txt").write_text(render_report(report) + "\n")
    (out / "report.json").write_text(json.dumps(report, indent=2, default=str) + "\n")
    summary = f"{len(cards)} card(s), {len(trips)} trip(s), {len(orders)} order(s)"
    if PROVIDERS[a.provider]["receipts"] and not getattr(a, "no_receipts", False):
        saved, skipped, failed = save_receipts(c, orders, out / "receipts")
        summary += f", {saved + skipped} receipt(s)" + (f" ({failed} unavailable)" if failed else "")
    if getattr(a, "statements", False):
        summary += f", {save_statements(c, since, until, a.card, out / 'statements')} statement PDF(s)"
    note(f"exported {summary} for {since} to {until} into {out}")
    return 0


# --------------------------------------------------------------------------- parser

def _add_global_options(p: argparse.ArgumentParser) -> None:
    S = argparse.SUPPRESS  # so options work before or after the subcommand
    g = p.add_argument_group("account/output")
    g.add_argument("--username", default=S, help="account email (default: config file or $TRANSITLOG_CLIPPER_USERNAME)")
    g.add_argument("--password-file", default=S,
                   help="file whose first line is the password (default: ~/.config/transitlog/clipper.password)")
    g.add_argument("--config", default=S, help="config file (default: ~/.config/transitlog/config.ini)")
    g.add_argument("--timeout", type=int, default=S, help="request timeout in seconds (default: 30)")
    g.add_argument("--json", action="store_true", default=S, help="machine-readable JSON output")
    g.add_argument("--debug", action="store_true", default=S, help="log each request and raw errors to stderr")


def _add_range(p: argparse.ArgumentParser, card: bool = True) -> None:
    g = p.add_argument_group("which history")
    g.add_argument("--since", type=parse_date, metavar="YYYY-MM-DD",
                   help=f"first day (default: {DEFAULT_DAYS} days ago)")
    g.add_argument("--until", type=parse_date, metavar="YYYY-MM-DD",
                   help="last day (default: today, in the provider's local time)")
    g.add_argument("--days", type=int, metavar="N", help="the last N days, ending --until (ignored with --since)")
    if card:
        g.add_argument("--card", metavar="ID",
                       help="one card: account number, card number or last 4 digits (default: all)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="transitlog",
        description="Download and summarise your own transit-card history. Read-only: it never buys, "
                    "loads, disputes or changes anything.",
        epilog=textwrap.fill("Set a password with `transitlog <provider> set-password` (stored in "
                             "~/.config/transitlog/<provider>.password, mode 600) or set "
                             "$TRANSITLOG_<PROVIDER>_PASSWORD. Providers: " + ", ".join(PROVIDERS) + ".")
               + "\n\n" + textwrap.fill(DISCLAIMER_SHORT),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {__version__}\n{textwrap.fill(DISCLAIMER_SHORT)}")
    _add_global_options(parser)
    providers = parser.add_subparsers(dest="provider", required=True, metavar="PROVIDER")
    for name, spec in PROVIDERS.items():
        _add_provider(providers, name, spec)
    return parser


def _add_provider(providers: Any, name: str, spec: dict[str, Any]) -> None:
    prov = providers.add_parser(name, help=spec["help"], description=spec["desc"])
    _add_global_options(prov)
    sub = prov.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def add(cmd: str, func, help_text: str, login: bool = True) -> argparse.ArgumentParser:
        p = sub.add_parser(cmd, help=help_text, description=help_text)
        _add_global_options(p)
        p.set_defaults(func=func, login=login)
        return p

    add("cards", cmd_cards, "cards on the account, with balance and status")

    for cmd, func, what in (("trips", cmd_trips, "trip history"),
                            ("orders", cmd_orders, "purchases: cash-value loads, passes, card fees")):
        p = add(cmd, func, what)
        _add_range(p)
        p.add_argument("-o", "--output", metavar="FILE", help="save as CSV, or JSON if FILE ends in .json")

    if spec["receipts"]:
        p = add("receipts", cmd_receipts, "download a PDF receipt for every order (skips ones already saved)")
        _add_range(p)
        p.add_argument("-d", "--dir", default=f"{name}-receipts",
                       help=f"output directory (default: ./{name}-receipts)")

        p = add("statements", cmd_statements, "download the provider's official trip-history PDFs (one per month)")
        _add_range(p)
        p.add_argument("-d", "--dir", default=f"{name}-statements",
                       help=f"output directory (default: ./{name}-statements)")

    p = add("report", cmd_report, "usage summary: trips by operator/month/weekday, top stations, spending, pass value")
    _add_range(p)

    p = add("export", cmd_export, "everything into one folder: cards, trips and orders (CSV+JSON), report, receipts")
    _add_range(p)
    p.add_argument("-d", "--dir", default=f"{name}-export", help=f"output directory (default: ./{name}-export)")
    if spec["receipts"]:
        p.add_argument("--no-receipts", action="store_true", help="skip the per-order PDF receipts")
        p.add_argument("--statements", action="store_true", help="also save the official trip-history PDFs")

    p = add("set-password", None, f"save the {name} password to the password file (mode 600)", login=False)
    p.add_argument("--file", default=str(default_password_file(name)),
                   help=f"where to store it (default: ~/.config/transitlog/{name}.password)")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse and validate everything that can be checked before logging in."""
    parser = build_parser()
    args = parser.parse_args(argv)
    for name, default in GLOBAL_DEFAULTS.items():
        if not hasattr(args, name):
            setattr(args, name, default)
    if getattr(args, "days", None) is not None and args.days < 1:
        parser.error("--days must be at least 1")
    if getattr(args, "since", None) and getattr(args, "until", None) and args.since > args.until:
        parser.error("--since is after --until")
    return args


def _run(args: argparse.Namespace, logger: logging.Logger | None) -> int:
    if args.command == "set-password":
        first = getpass.getpass(f"{args.provider.capitalize()} password: ")
        if not first or first != getpass.getpass("Again: "):
            note("error: passwords empty or did not match")
            return 2
        print(f"saved to {write_password_file(first, args.file)}")
        return 0

    credentials = resolve_credentials(args.provider, args.username, args.password_file, args.config)
    # Re-read the class from this module so tests can monkeypatch it by name.
    client_cls = getattr(sys.modules[__name__], PROVIDERS[args.provider]["client"].__name__)
    with client_cls(credentials, timeout=args.timeout, logger=logger) as client:
        code = args.func(client, args)
        for warning in client.warnings:
            note(f"warning: {warning}")
        return code


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    logger = logging.getLogger("transitlog") if args.debug else None
    if not args.debug:
        logging.getLogger("urllib3").setLevel(logging.WARNING)
    try:
        code = _run(args, logger)
        sys.stdout.flush()  # surface a closed pipe here rather than during interpreter shutdown
        return code
    except ConfigError as e:
        note(f"error: {e}")
        return 2
    except TransitError as e:
        note(f"error: {e}")
        if args.debug and e.raw is not None:
            note(f"raw error: {e.raw!r}")
        return 1
    except BrokenPipeError:  # output piped into e.g. `head`, which exited early
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        if args.debug:
            raise
        note(f"error: {type(e).__name__}: {e} (re-run with --debug for details)")
        return 1


if __name__ == "__main__":
    sys.exit(main())
