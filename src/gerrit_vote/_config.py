# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Junbo Zheng. All rights reserved.
"""Credential resolution for Gerrit hosts.

Resolution order, first hit wins:

1. Environment variables (single host):
   ``GERRIT_URL`` / ``GERRIT_USER`` (or ``GERRIT_USERNAME``) /
   ``GERRIT_HTTP_PASSWORD`` (or ``GERRIT_PASSWORD``).
2. Config file ``~/.config/gerrit-vote/config.toml`` (override with
   ``$GERRIT_VOTE_CONFIG``), mapping each host to its credentials.

Config file format::

    default_host = "gerrit.example.com"

    [hosts."gerrit.example.com"]
    url = "https://gerrit.example.com"
    user = "alice"
    password = "<gerrit-http-password>"

The HTTP password is generated at Gerrit -> Settings -> HTTP Credentials.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised only on 3.10
    import tomli as tomllib


class ConfigError(Exception):
    """Credentials could not be resolved for a host."""


@dataclass(frozen=True)
class Credentials:
    url: str  # base URL, no trailing slash, e.g. https://gerrit.example.com
    user: str
    password: str

    @property
    def host(self) -> str:
        return urlparse(self.url).hostname or ""


def _config_path() -> Path:
    override = os.environ.get("GERRIT_VOTE_CONFIG")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(base).expanduser() / "gerrit-vote" / "config.toml"


def _host_of(url: str) -> str:
    return urlparse(url if "://" in url else "https://" + url).hostname or ""


def _from_env() -> Credentials | None:
    url = (os.environ.get("GERRIT_URL") or "").strip()
    user = (
        os.environ.get("GERRIT_USER") or os.environ.get("GERRIT_USERNAME") or ""
    ).strip()
    pw = (
        os.environ.get("GERRIT_HTTP_PASSWORD")
        or os.environ.get("GERRIT_PASSWORD")
        or ""
    ).strip()
    if url and user and pw:
        return Credentials(url=url.rstrip("/"), user=user, password=pw)
    return None


def _load_file() -> tuple[dict[str, Credentials], str | None]:
    """Return ({host: Credentials}, default_host) from the config file."""
    path = _config_path()
    if not path.is_file():
        return {}, None
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"Bad config file {path}: {e}") from e

    out: dict[str, Credentials] = {}
    for host, c in (data.get("hosts") or {}).items():
        try:
            url = (c.get("url") or f"https://{host}").rstrip("/")
            out[host] = Credentials(url=url, user=c["user"], password=c["password"])
        except (KeyError, AttributeError) as e:
            raise ConfigError(
                f"Config host '{host}' missing 'user'/'password' ({e})."
            ) from e
    return out, data.get("default_host")


def resolve(
    host: str | None, *, user: str | None = None, password: str | None = None
) -> Credentials:
    """Resolve credentials for ``host`` (None = bare change number).

    ``user`` / ``password`` are command-line overrides: when given they win over
    env vars and the config file for that field. Providing both lets you vote
    with no env/config at all, as long as the URL is determinable (a full change
    URL supplies the host, or set ``GERRIT_URL`` / ``default_host``). A single
    identity applies across a multi-host batch — each host keeps its own URL but
    uses the overridden user/password.
    """
    env = _from_env()
    file_hosts, default_host = _load_file()

    if user or password:
        # Override path: derive a URL, then fill any missing half from stored creds.
        try:
            base: Credentials | None = _resolve_stored(
                env, file_hosts, default_host, host
            )
        except ConfigError:
            base = None
        url = base.url if base else _url_for(host, env, file_hosts, default_host)
        if url is None:
            raise ConfigError(
                "Cannot determine the Gerrit URL for --user/--password. "
                "Pass a full change URL, or set GERRIT_URL / default_host."
            )
        final_user = user or (base.user if base else None)
        final_pw = password or (base.password if base else None)
        if not final_user or not final_pw:
            raise ConfigError(
                "Incomplete credentials: provide both --user and --password, "
                "or supply the missing half via env/config."
            )
        return Credentials(url=url.rstrip("/"), user=final_user, password=final_pw)

    return _resolve_stored(env, file_hosts, default_host, host)


def all_hosts(
    *, user: str | None = None, password: str | None = None
) -> list[Credentials]:
    """Return credentials for every configured Gerrit host (env + config file).

    Used by the CLI's ``--all`` mode to sweep the caller's open changes across
    all hosts at once. The env-var host (if set) and every ``[hosts."..."]``
    entry are merged, de-duplicated by host. ``user`` / ``password`` overrides
    apply uniformly (same identity across the batch), mirroring resolve().
    Raises ConfigError only when nothing is configured at all.
    """
    env = _from_env()
    file_hosts, _ = _load_file()

    merged: dict[str, Credentials] = dict(file_hosts)
    if env is not None:
        merged.setdefault(env.host, env)

    if not merged:
        raise ConfigError(_no_creds_help(None))

    if user or password:
        out: list[Credentials] = []
        for c in merged.values():
            out.append(
                Credentials(
                    url=c.url,
                    user=user or c.user,
                    password=password or c.password,
                )
            )
        return out
    return list(merged.values())


def _resolve_stored(
    env: Credentials | None,
    file_hosts: dict[str, Credentials],
    default_host: str | None,
    host: str | None,
) -> Credentials:
    """Resolve credentials from env + config file only (no CLI overrides)."""
    target = host or default_host
    # With env creds and no host hint (or a matching host), prefer env.
    if env is not None and (target is None or env.host == target):
        return env

    if target is None:
        # Bare number, no default: only unambiguous when exactly one source.
        candidates = dict(file_hosts)
        if env is not None:
            candidates[env.host] = env
        if len(candidates) == 1:
            return next(iter(candidates.values()))
        if not candidates:
            raise ConfigError(_no_creds_help(None))
        raise ConfigError(
            "Bare change number but multiple Gerrit hosts configured; "
            "pass the full change URL so the host is unambiguous."
        )

    if target in file_hosts:
        return file_hosts[target]
    if env is not None and env.host == target:
        return env

    raise ConfigError(_no_creds_help(target))


def _url_for(
    host: str | None,
    env: Credentials | None,
    file_hosts: dict[str, Credentials],
    default_host: str | None,
) -> str | None:
    """Best-effort base URL for a host, used by the --user/--password path.

    A configured host keeps its custom url (scheme/port); otherwise a known host
    becomes ``https://{host}``. Falls back to env GERRIT_URL or default_host.
    """
    target = host or default_host
    if target and target in file_hosts:
        return file_hosts[target].url
    if env is not None and (target is None or env.host == target):
        return env.url
    if target:
        return f"https://{target}"
    return None


def _no_creds_help(host: str | None) -> str:
    h = host or "gerrit.example.com"
    return (
        f"No credentials for host '{host or '(none)'}'.\n"
        "Set them via environment variables:\n"
        f"  export GERRIT_URL=https://{h}\n"
        "  export GERRIT_USER=<your-username>\n"
        "  export GERRIT_HTTP_PASSWORD=<gerrit-http-password>\n"
        f"or add a host to {_config_path()} :\n"
        f'  [hosts."{h}"]\n'
        f'  url = "https://{h}"\n'
        '  user = "<your-username>"\n'
        '  password = "<gerrit-http-password>"\n'
        "(HTTP password: Gerrit -> Settings -> HTTP Credentials)"
    )
