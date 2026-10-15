# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Amol Venkataraman
"""Exceptions, and one-line explanations for network failures."""
from __future__ import annotations

import requests


class TransitError(Exception):
    """A provider request failed. `str(e)` explains it; `e.raw` keeps the original error or response text."""

    def __init__(self, message: str, raw: object = None):
        super().__init__(message)
        self.raw = raw


class LoginFailed(TransitError):
    """The provider rejected the username/password. Never retried automatically."""


class UnsafeRequest(TransitError):
    """A request to an endpoint outside the client's read-only allowlist was attempted."""


def explain_network(exc: requests.RequestException, where: str) -> str:
    # Subclasses first: SSLError and ConnectTimeout are both ConnectionErrors.
    if isinstance(exc, requests.exceptions.SSLError):
        return f"TLS certificate check failed for {where}; check the system clock and any intercepting proxy"
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return f"timed out connecting to {where}; check your internet connection"
    if isinstance(exc, requests.exceptions.ReadTimeout):
        return f"{where} didn't answer in time; the site may be slow or down for maintenance (try --timeout 60)"
    if isinstance(exc, requests.exceptions.ConnectionError):
        return f"cannot connect to {where}; check your internet connection"
    return f"request to {where} failed: {exc}"
