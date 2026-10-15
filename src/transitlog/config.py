# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Amol Venkataraman
"""Resolve per-provider credentials from CLI flags, environment and config file.

Precedence (highest first): explicit arguments, environment variables, the
config file (~/.config/transitlog/config.ini, one [section] per provider).

The password is never taken from a command-line flag (it would leak into shell
history and `ps`). It comes from, in order: TRANSITLOG_<PROVIDER>_PASSWORD, a
password file named by --password-file / TRANSITLOG_<PROVIDER>_PASSWORD_FILE /
the config file, or the default ~/.config/transitlog/<provider>.password.
"""
from __future__ import annotations

import configparser
import os
import stat
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "transitlog"
CONFIG_FILE = CONFIG_DIR / "config.ini"


class ConfigError(Exception):
    """Credentials could not be resolved (no username, missing/empty password file, bad config)."""


@dataclass(frozen=True)
class Credentials:
    username: str
    password: str

    def __repr__(self) -> str:  # keep the password out of logs and tracebacks
        return f"Credentials(username={self.username!r}, password='***')"


def default_password_file(provider: str) -> Path:
    return CONFIG_DIR / f"{provider}.password"


def read_password_file(path: str | Path) -> str:
    """Return the first line of `path`, warning if other users can read it."""
    path = Path(path).expanduser()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(f"password file not found: {path} "
                          "(create it with `transitlog <provider> set-password`)") from None
    except OSError as e:
        raise ConfigError(f"cannot read password file {path}: {e}") from None
    if path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        print(f"warning: {path} is accessible by other users; run: chmod 600 {path}", file=sys.stderr)
    lines = text.splitlines()
    password = lines[0] if lines else ""
    if not password:
        raise ConfigError(f"password file is empty: {path}")
    return password


def write_password_file(password: str, path: str | Path) -> Path:
    """Store `password` in `path` with mode 0600 (directory 0700)."""
    path = Path(path).expanduser()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(password + "\n")
    os.chmod(path, 0o600)  # O_CREAT's mode is ignored if the file already existed
    return path


def load_config_section(provider: str, path: str | Path = CONFIG_FILE) -> dict[str, str]:
    """Return the [provider] section of the INI config file, or {} if absent."""
    path = Path(path).expanduser()
    if not path.exists():
        return {}
    parser = configparser.ConfigParser()
    try:
        parser.read(path, encoding="utf-8")
    except configparser.Error as e:
        raise ConfigError(f"invalid config file {path}: {e}") from None
    return dict(parser[provider]) if parser.has_section(provider) else {}


def resolve_credentials(
    provider: str,
    username: str | None = None,
    password_file: str | Path | None = None,
    config_file: str | Path = CONFIG_FILE,
    env: Mapping[str, str] = os.environ,
) -> Credentials:
    cfg = load_config_section(provider, config_file)
    prefix = f"TRANSITLOG_{provider.upper()}_"

    username = username or env.get(prefix + "USERNAME") or cfg.get("username")
    if not username:
        raise ConfigError(f"no {provider} username: pass --username, set ${prefix}USERNAME, "
                          f"or add `username = ...` under [{provider}] in {config_file}")

    if password_file is None and env.get(prefix + "PASSWORD"):
        password = env[prefix + "PASSWORD"]
    else:
        source = (password_file or env.get(prefix + "PASSWORD_FILE") or cfg.get("password_file")
                  or default_password_file(provider))
        password = read_password_file(source)
    return Credentials(username, password)
