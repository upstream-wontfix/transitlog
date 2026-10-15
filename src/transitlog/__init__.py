# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Amol Venkataraman
"""Download and summarise your own transit-card trip and purchase history.

Provided AS IS, without warranty of any kind; see LICENSE and DISCLAIMER.md.
"""
__version__ = "0.1.0"

from .clipper import ClipperClient
from .config import ConfigError, Credentials, resolve_credentials
from .errors import LoginFailed, TransitError, UnsafeRequest
from .models import Card, Order, Trip
from .presto import PrestoClient
from .report import build_report, render_report

__all__ = [
    "Card",
    "ClipperClient",
    "ConfigError",
    "Credentials",
    "LoginFailed",
    "Order",
    "PrestoClient",
    "TransitError",
    "Trip",
    "UnsafeRequest",
    "__version__",
    "build_report",
    "render_report",
    "resolve_credentials",
]
