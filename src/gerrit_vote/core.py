# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Junbo Zheng. All rights reserved.
"""Core Gerrit change operations and change-link extraction.

Voting (the default) only touches the ``labels`` field via the SetReview
endpoint and NEVER submits or merges a change. The module also provides opt-in
lifecycle actions — abandon / restore / delete — used behind their own CLI
flags. ``delete_change`` is irreversible; the rest are reversible.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlparse

from .config import Credentials, resolve

GERRIT_MAGIC = ")]}'"

# CJK full-width punctuation that commonly trails a URL in Chinese chat logs.
_CJK_PUNCT = "。，、；:！？（）【】《》「」『』“”‘’…　"
_URL_RE = re.compile(rf"https?://[^\s,<>\"')\]{_CJK_PUNCT}]+", re.IGNORECASE)
_TRAILING = ".,;:)]}>\"'" + _CJK_PUNCT

# A Gerrit Change-Id: uppercase "I" + 40 hex chars (git's Change-Id format).
# Appears in /q/I<id> query URLs, which carry no numeric change number — they
# must be resolved to /c/<number> via the REST API before vote() can consume
# them. Recognized here so extract_changes keeps them.
_CHANGE_ID_RE = re.compile(r"^I[0-9a-fA-F]{40}$")


class VoteError(Exception):
    """Fatal error during voting."""


# ---------------------------------------------------------------------------
# Change-link extraction
# ---------------------------------------------------------------------------


def parse_change(arg: str) -> tuple[str | None, str]:
    """Return (host, change_id) from a Gerrit URL or bare change number.

    host is None when only a bare number was given.
    """
    arg = arg.strip()
    if arg.startswith(("http://", "https://")):
        parsed = urlparse(arg)
        host = parsed.hostname or None
        parts = [p for p in parsed.path.split("/") if p]
        if "+" in parts:
            idx = parts.index("+")
            if idx + 1 < len(parts) and parts[idx + 1].isdigit():
                return host, parts[idx + 1]
        for p in reversed(parts):
            if p.isdigit():
                return host, p
        raise VoteError(f"Cannot find change id in URL: {arg}")
    if not arg.isdigit():
        raise VoteError(f"Not a URL and not a numeric change id: {arg}")
    return None, arg


def _canonical(url: str) -> str | None:
    url = url.rstrip(_TRAILING)
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if "gerrit" not in host.lower():
        return None
    parts = [p for p in parsed.path.split("/") if p]
    if "+" in parts:
        idx = parts.index("+")
        if idx + 1 < len(parts) and parts[idx + 1].isdigit():
            return f"{parsed.scheme}://{host}/" + "/".join(parts[: idx + 2])
        return None
    if len(parts) == 1 and parts[0].isdigit():
        return f"{parsed.scheme}://{host}/{parts[0]}"
    if len(parts) >= 2 and parts[0] == "c" and parts[1].isdigit():
        return f"{parsed.scheme}://{host}/c/{parts[1]}"
    # /q/I<Change-Id>: a Change-Id query URL, not a change number. Keep it
    # canonical so extract_changes preserves it; resolve_change_ids() rewrites
    # it to /c/<number> (via the REST API) before vote() is called.
    if len(parts) == 2 and parts[0] == "q" and _CHANGE_ID_RE.match(parts[1]):
        return f"{parsed.scheme}://{host}/q/{parts[1]}"
    return None


def _fragment_change(url: str) -> str | None:
    m = re.search(r"https?://([^/\s]+)/#/c/(\d+)", url, re.IGNORECASE)
    if m and "gerrit" in m.group(1).lower():
        return f"https://{m.group(1)}/c/{m.group(2)}"
    return None


def dedup_changes(urls: list[str]) -> list[str]:
    """De-duplicate change URLs by (host, change_id), first-seen order.

    The same change can appear as ``/c/proj/path/+/N`` or ``/c/N`` — different
    URL strings, the same change. A plain string dedup leaves both, so a single
    change gets voted twice. This collapses them on the semantic key. A URL that
    cannot be parsed (no numeric change id) falls back to raw-string dedup.
    """
    by_key: dict[tuple[str, str], str] = {}
    for u in urls:
        try:
            host, change_id = parse_change(u)
            key = (host or "", change_id)
        except VoteError:
            key = ("", u)  # unparseable: dedup by raw string
        by_key.setdefault(key, u)
    return list(by_key.values())


def extract_changes(text: str) -> list[str]:
    """Extract canonical Gerrit change URLs from arbitrary text.

    De-duplicated, first-seen order. Different hosts are all kept — a mixed
    batch (pt + odm + any third-party gerrit) is preserved as-is, and each URL
    later resolves its own host's credentials independently.
    """
    found: list[str] = []
    for raw in _URL_RE.findall(text):
        canon = _fragment_change(raw) or _canonical(raw)
        if canon:
            found.append(canon)
    return dedup_changes(found)


def _change_id_from_url(url: str) -> str | None:
    """Return the Change-Id from a /q/I<id> Gerrit URL, else None."""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if "gerrit" not in host.lower():
        return None
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) == 2 and parts[0] == "q" and _CHANGE_ID_RE.match(parts[1]):
        return parts[1]
    return None


def resolve_change_ids(
    urls: list[str],
    *,
    user: str | None = None,
    password: str | None = None,
) -> tuple[list[str], list[str]]:
    """Resolve any /q/I<Change-Id> URLs to /c/<number> form.

    A /q/I... URL carries a Change-Id, not a change number — parse_change()
    and vote() cannot consume it directly. This queries each such URL's host
    (``change:<id> status:open``) and rewrites it to ``{url}/c/<number>``.
    Plain /c/N URLs pass through unchanged. The result is de-duplicated, so a
    /q/I... that resolves to a change already present as /c/N collapses.

    Returns ``(resolved_urls, warnings)``. A Change-Id with zero open matches
    is dropped from resolved_urls and reported in warnings (the rest of the
    batch proceeds). A Change-Id with multiple open matches — the normal case
    for a cross-branch cherry-pick, which keeps one Change-Id across branches —
    is expanded to ALL of them (mirroring topic/related expansion, so a
    cross-branch patch is voted on every branch consistently). A network/
    auth/host-config failure raises VoteError/ConfigError (propagated).
    """
    resolved: list[str] = []
    warnings: list[str] = []
    for u in urls:
        cid = _change_id_from_url(u)
        if cid is None:
            resolved.append(u)
            continue
        host = urlparse(u).hostname or ""
        creds = resolve(host, user=user, password=password)
        query = quote(f"change:{cid} status:open")
        status, body = _request(
            "GET", f"{creds.url}/a/changes/?q={query}", creds.auth, None
        )
        if status != 200:
            raise VoteError(
                f"Cannot resolve Change-Id {cid} on {creds.host} (HTTP {status}). "
                + _http_hint(status)
            )
        matches = [c for c in json.loads(_strip_magic(body)) if "_number" in c]
        if not matches:
            warnings.append(
                f"Change-Id {cid} on {creds.host}: no open change matched, skipped"
            )
            continue
        for c in matches:
            resolved.append(f"{creds.url}/c/{c['_number']}")
        if len(matches) > 1:
            nums = ", ".join(str(c["_number"]) for c in matches)
            warnings.append(
                f"Change-Id {cid} on {creds.host}: resolved to "
                f"{len(matches)} open changes ({nums}) — voting on all"
            )
    return dedup_changes(resolved), warnings


# ---------------------------------------------------------------------------
# Topic extraction
# ---------------------------------------------------------------------------

# A Gerrit topic query URL: canonical "/q/topic:NAME" or legacy "/#/q/topic:NAME".
# The name runs to the end of the captured URL token (trailing punctuation is
# stripped first), and may be percent-encoded and/or wrapped in quotes.
_TOPIC_RE = re.compile(r"/(?:#/)?q/topic:(.+)$", re.IGNORECASE)

# Topic URLs carry a colon in "topic:NAME", which _URL_RE deliberately excludes
# (a trailing ASCII colon is common in chat). So scan topic URLs as whole tokens
# delimited by whitespace OR a comma; parse_topic then strips trailing
# punctuation and validates the form. The comma stop matters for CSV/table input
# ("...topic:NAME,col2,col3..."), where \S+ would otherwise swallow the trailing
# columns into the topic name.
_TOPIC_SCAN_RE = re.compile(r"https?://[^\s,]+", re.IGNORECASE)


def parse_topic(arg: str) -> tuple[str | None, str] | None:
    """Return (host, topic_name) from a Gerrit topic query URL, else None.

    Accepts the canonical ``https://host/q/topic:NAME`` and the legacy fragment
    ``https://host/#/q/topic:NAME`` forms. The name may be percent-encoded
    (``topic:%22foo%22``) or quoted (``topic:"foo"``); both are normalized. host
    is taken from the URL (always present for the URL form). Returns None for
    anything that is not a gerrit topic URL.
    """
    arg = arg.strip().rstrip(_TRAILING)
    if not arg.startswith(("http://", "https://")):
        return None
    parsed = urlparse(arg)
    host = parsed.hostname or ""
    if "gerrit" not in host.lower():
        return None
    # The fragment form keeps "topic:..." in the URL fragment, not the path.
    tail = parsed.path + (f"#{parsed.fragment}" if parsed.fragment else "")
    m = _TOPIC_RE.search(tail)
    if not m:
        return None
    name = unquote(m.group(1)).strip().strip('"')
    if not name:
        return None
    return host, name


def extract_topics(text: str) -> list[tuple[str, str]]:
    """Extract (host, topic_name) pairs from arbitrary text, first-seen order.

    De-duplicated on (host, name). Mirrors extract_changes for the topic-URL
    case; only URL hits are recognized (the bare ``--topic`` flag is handled by
    the CLI, not here).
    """
    seen: dict[tuple[str, str], None] = {}
    for raw in _TOPIC_SCAN_RE.findall(text):
        spec = parse_topic(raw)
        if spec and spec[0] is not None and spec not in seen:
            seen[(spec[0], spec[1])] = None
    return list(seen)


# ---------------------------------------------------------------------------
# Voting
# ---------------------------------------------------------------------------


def fmt(value: int) -> str:
    return f"+{value}" if value > 0 else str(value)


# Transient-failure retry: server 5xx and connection-level errors are often
# momentary (Gerrit restart, proxy blip), so retry a couple of times with a
# short linear backoff. 4xx are NOT retried — they are deterministic (bad auth,
# not permitted, no such change) and retrying only delays the real error.
_RETRIES = 2  # extra attempts after the first (3 tries total)
_BACKOFF = 0.5  # seconds; multiplied by the attempt number for a linear backoff


def _request(method: str, url: str, auth: str, payload: dict | None) -> tuple[int, str]:
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Authorization": "Basic " + auth}
    if data is not None:
        headers["Content-Type"] = "application/json; charset=UTF-8"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)

    for attempt in range(_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            # An HTTP error still carries a status + body we can report on. Only
            # 5xx is transient; 4xx is deterministic, so return it immediately.
            if e.code >= 500 and attempt < _RETRIES:
                time.sleep(_BACKOFF * (attempt + 1))
                continue
            return e.code, e.read().decode(errors="replace")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            # No HTTP response at all: DNS failure, refused connection, timeout,
            # TLS error, etc. Retry, then surface as a clean VoteError.
            if attempt >= _RETRIES:
                reason = getattr(e, "reason", e)
                raise VoteError(
                    f"network error talking to {urlparse(url).hostname}: {reason}"
                ) from e
            time.sleep(_BACKOFF * (attempt + 1))


def _strip_magic(body: str) -> str:
    return body.split("\n", 1)[1] if body.startswith(GERRIT_MAGIC) else body


def _http_hint(status: int) -> str:
    """A likely-cause hint for a non-200 Gerrit status.

    The old code appended "Credentials expired or no access" to every failure,
    which mis-blamed credentials for a 404 (the change simply does not exist).
    Map the common codes to an accurate cause instead.
    """
    return {
        401: "Credentials expired (authentication failed).",
        403: "No permission for this change.",
        404: "Change does not exist or was deleted (or is not visible to you).",
    }.get(status, "Unexpected server response.")


def git_identity_email() -> str | None:
    """Return the user's global git email (~/.gitconfig user.email), or None.

    Uses ``git config --global`` so a repo-local override in the current
    directory does not shadow the developer's real Gerrit identity. Returns
    None when git is absent or user.email is unset.
    """
    try:
        out = subprocess.run(
            ["git", "config", "--global", "--get", "user.email"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    email = out.stdout.strip()
    return email or None


@dataclass
class VoteResult:
    change_id: str
    host: str
    subject: str
    project: str
    branch: str
    status: str  # gerrit change status (NEW/MERGED/ABANDONED/...)
    requested: int
    cast: int | None  # value actually cast; None on dry-run or skip
    downgraded: bool
    dry_run: bool
    label: str = "Code-Review"  # the label this vote targets
    skipped: bool = False  # closed change (MERGED/ABANDONED): no vote sent
    skip_reason: str = ""
    owner_email: str = ""  # who pushed the change (from DETAILED_ACCOUNTS)
    author_email: str = ""  # who wrote the current commit (from CURRENT_COMMIT)
    labels: dict[str, int] | None = None  # all labels cast (multi-label votes)
    action: str = "vote"  # "vote" | "abandon" | "delete" — drives the CLI display


def vote(
    change: str,
    value: int = 2,
    *,
    label: str = "Code-Review",
    message: str | None = None,
    dry_run: bool = False,
    fallback: bool = True,
    creds: Credentials | None = None,
    user: str | None = None,
    password: str | None = None,
) -> VoteResult:
    """Cast a label vote on one change. Never submits.

    Resolves credentials per the change URL's host unless ``creds`` is given.
    ``user`` / ``password`` are passed through to resolve() as command-line
    overrides (ignored when ``creds`` is supplied directly).
    """
    host, change_id = parse_change(change)
    if creds is None:
        creds = resolve(host, user=user, password=password)
    auth = creds.auth
    base = creds.url

    status, body = _request(
        "GET",
        f"{base}/a/changes/{change_id}"
        "?o=CURRENT_REVISION&o=CURRENT_COMMIT&o=DETAILED_LABELS&o=DETAILED_ACCOUNTS",
        auth,
        None,
    )
    if status != 200:
        raise VoteError(
            f"Cannot read change {change_id} on {creds.host} (HTTP {status}). "
            + _http_hint(status)
        )
    info = json.loads(_strip_magic(body))

    requested = value
    downgraded = False
    change_status = info.get("status", "")

    # A closed change can't take a meaningful review vote — skip MERGED /
    # ABANDONED instead of POSTing a no-op (or error) the user has to read past.
    if change_status in ("MERGED", "ABANDONED"):
        return VoteResult(
            change_id=str(info.get("_number", change_id)),
            host=creds.host,
            subject=info.get("subject", ""),
            project=info.get("project", ""),
            branch=info.get("branch", ""),
            status=change_status,
            requested=requested,
            cast=None,
            downgraded=False,
            dry_run=dry_run,
            label=label,
            skipped=True,
            skip_reason=f"already {change_status.lower()}",
            owner_email=(info.get("owner") or {}).get("email", ""),
            author_email=_author_email(info),
        )

    permitted = (info.get("permitted_labels") or {}).get(label)
    # Gerrit pads permitted-label strings for alignment (" 0" for zero, so it
    # lines up with "-1"/"+1"), so compare against stripped values, not the raw
    # strings — otherwise fmt(0) == "0" never matches the padded " 0".
    permitted_set = {p.strip() for p in permitted} if permitted is not None else None
    if permitted_set is not None and fmt(value) not in permitted_set:
        if fallback and value == 2 and "+1" in permitted_set:
            value = 1
            downgraded = True
        else:
            allowed = ",".join(permitted) if permitted else "(none)"
            raise VoteError(
                f"{label}={fmt(requested)} not permitted on change {change_id}. "
                f"Allowed: [{allowed}]." + ("" if fallback else " (fallback off)")
            )

    result = VoteResult(
        change_id=str(info.get("_number", change_id)),
        host=creds.host,
        subject=info.get("subject", ""),
        project=info.get("project", ""),
        branch=info.get("branch", ""),
        status=info.get("status", ""),
        requested=requested,
        cast=None if dry_run else value,
        downgraded=downgraded,
        dry_run=dry_run,
        label=label,
        owner_email=(info.get("owner") or {}).get("email", ""),
        author_email=_author_email(info),
        labels={label: value},
    )
    if dry_run:
        return result

    payload: dict = {"labels": {label: value}}
    if message:
        payload["message"] = message
    status, body = _request(
        "POST",
        f"{base}/a/changes/{change_id}/revisions/current/review",
        auth,
        payload,
    )
    if status != 200:
        raise VoteError(
            f"Vote failed on {change_id} (HTTP {status}): {_strip_magic(body)[:300]}"
        )
    return result


def _author_email(info: dict) -> str:
    """Pull the current revision's git author email from a change info dict.

    Requires the change to have been fetched with o=CURRENT_REVISION and
    o=CURRENT_COMMIT. Returns "" when the commit/author is absent (e.g. the
    detail level was not requested). This is the person who *wrote* the commit,
    which can differ from the owner (who pushed it) — e.g. after a cherry-pick.
    """
    revisions = info.get("revisions") or {}
    for rev in revisions.values():
        commit = rev.get("commit") or {}
        author = commit.get("author") or {}
        email = author.get("email")
        if email:
            return email
    return ""


@dataclass
class ChangeMeta:
    permitted: dict[str, list[str]]  # {label: [allowed values]}, padding stripped
    owner_email: str  # who pushed the change
    author_email: str  # who wrote the current commit
    subject: str
    status: str  # NEW / MERGED / ABANDONED / ...


def change_meta(
    change: str,
    *,
    creds: Credentials | None = None,
    user: str | None = None,
    password: str | None = None,
) -> ChangeMeta:
    """Return a ChangeMeta (permitted labels, owner/author email, subject, status).

    Fetched in one GET. Used by the interactive menu to build value rows
    (permitted), decide whether to offer owner-only combo actions (owner_email
    vs the local git identity), and show subject/author before voting — without
    paying extra round-trips. Permitted values are stripped of Gerrit's padding.
    """
    host, change_id = parse_change(change)
    if creds is None:
        creds = resolve(host, user=user, password=password)
    auth = creds.auth
    status_code, body = _request(
        "GET",
        f"{creds.url}/a/changes/{change_id}"
        "?o=DETAILED_LABELS&o=DETAILED_ACCOUNTS&o=CURRENT_REVISION&o=CURRENT_COMMIT",
        auth,
        None,
    )
    if status_code != 200:
        raise VoteError(
            f"Cannot read change {change_id} on {creds.host} (HTTP {status_code}). "
            + _http_hint(status_code)
        )
    info = json.loads(_strip_magic(body))
    permitted = info.get("permitted_labels") or {}
    stripped = {n: [v.strip() for v in vals] for n, vals in permitted.items()}
    return ChangeMeta(
        permitted=stripped,
        owner_email=(info.get("owner") or {}).get("email", ""),
        author_email=_author_email(info),
        subject=info.get("subject", ""),
        status=info.get("status", ""),
    )


def vote_labels(
    change: str,
    labels: dict[str, int],
    *,
    message: str | None = None,
    dry_run: bool = False,
    require_owner_email: str | None = None,
    creds: Credentials | None = None,
    user: str | None = None,
    password: str | None = None,
) -> VoteResult:
    """Cast several labels on one change in a single atomic POST. Never submits.

    All of ``labels`` are applied together (one review, one comment) — this is
    the "combo action" used by the interactive menu (e.g. Code-Review +1 +
    Presubmit-Ready +1 + Auto-Submit +1). It is all-or-nothing: if the server's
    permitted_labels does not allow every requested label/value, the whole
    change is skipped (no partial vote). MERGED/ABANDONED changes are skipped
    too, mirroring vote().
    """
    host, change_id = parse_change(change)
    if creds is None:
        creds = resolve(host, user=user, password=password)
    auth = creds.auth
    base = creds.url

    status, body = _request(
        "GET",
        f"{base}/a/changes/{change_id}"
        "?o=CURRENT_REVISION&o=CURRENT_COMMIT&o=DETAILED_LABELS&o=DETAILED_ACCOUNTS",
        auth,
        None,
    )
    if status != 200:
        raise VoteError(
            f"Cannot read change {change_id} on {creds.host} (HTTP {status}). "
            + _http_hint(status)
        )
    info = json.loads(_strip_magic(body))
    change_status = info.get("status", "")
    owner_email = (info.get("owner") or {}).get("email", "")
    author_email = _author_email(info)

    def _skip(reason: str) -> VoteResult:
        return VoteResult(
            change_id=str(info.get("_number", change_id)),
            host=creds.host,
            subject=info.get("subject", ""),
            project=info.get("project", ""),
            branch=info.get("branch", ""),
            status=change_status,
            requested=0,
            cast=None,
            downgraded=False,
            dry_run=dry_run,
            label=", ".join(labels),
            skipped=True,
            skip_reason=reason,
            owner_email=owner_email,
            author_email=author_email,
            labels=dict(labels),
        )

    if change_status in ("MERGED", "ABANDONED"):
        return _skip(f"already {change_status.lower()}")

    # Owner gate: combo actions (Presubmit-Ready / Auto-Submit) are only cast on
    # the caller's own changes. Re-checked here per change, not just in the menu.
    if require_owner_email is not None:
        if owner_email.lower() != require_owner_email.lower():
            return _skip(
                f"not owned by you ({owner_email or 'unknown'}); combo not cast"
            )

    # All-or-nothing: every requested label/value must be permitted, else the
    # whole change is skipped rather than casting a partial combo.
    permitted_all = info.get("permitted_labels") or {}
    for name, val in labels.items():
        permitted = permitted_all.get(name)
        permitted_set = {p.strip() for p in permitted} if permitted else set()
        if fmt(val) not in permitted_set:
            allowed = ",".join(permitted) if permitted else "(none)"
            return _skip(
                f"{name}={fmt(val)} not permitted (allowed: [{allowed}]); "
                "combo not cast"
            )

    result = VoteResult(
        change_id=str(info.get("_number", change_id)),
        host=creds.host,
        subject=info.get("subject", ""),
        project=info.get("project", ""),
        branch=info.get("branch", ""),
        status=change_status,
        requested=0,
        cast=None if dry_run else 0,
        downgraded=False,
        dry_run=dry_run,
        label=", ".join(labels),
        owner_email=owner_email,
        author_email=author_email,
        labels=dict(labels),
    )
    if dry_run:
        return result

    payload: dict = {"labels": dict(labels)}
    if message:
        payload["message"] = message
    status, body = _request(
        "POST",
        f"{base}/a/changes/{change_id}/revisions/current/review",
        auth,
        payload,
    )
    if status != 200:
        raise VoteError(
            f"Vote failed on {change_id} (HTTP {status}): {_strip_magic(body)[:300]}"
        )
    return result


def changes_in_topic(creds: Credentials, topic: str) -> list[str]:
    """Return canonical change URLs for every open change under ``topic``.

    Queries the host with ``topic:NAME status:open`` so merged/abandoned changes
    are filtered server-side. The returned ``{url}/c/{number}`` URLs are accepted
    by parse_change(), so they feed straight back into vote().
    """
    auth = creds.auth
    query = quote(f'topic:"{topic}" status:open')
    status, body = _request("GET", f"{creds.url}/a/changes/?q={query}", auth, None)
    if status != 200:
        raise VoteError(
            f"Cannot query topic '{topic}' on {creds.host} (HTTP {status}). "
            + _http_hint(status)
        )
    changes = json.loads(_strip_magic(body))
    return [f"{creds.url}/c/{c['_number']}" for c in changes if "_number" in c]


def my_changes(creds: Credentials, *, status: str = "open") -> list[str]:
    """Return canonical change URLs for changes owned by the caller.

    Queries ``owner:self status:<status>`` on ``creds``' host — "self" is
    resolved server-side from the authenticated user. ``status`` is the Gerrit
    status token (``open`` for the abandon/delete sweep, ``abandoned`` for the
    restore sweep). The ``{url}/c/{number}`` URLs feed straight back into
    vote() / abandon() / delete_change() / restore(). Used by ``--all``.
    """
    auth = creds.auth
    query = quote(f"owner:self status:{status}")
    status_code, body = _request("GET", f"{creds.url}/a/changes/?q={query}", auth, None)
    if status_code != 200:
        raise VoteError(
            f"Cannot query your {status} changes on {creds.host} "
            f"(HTTP {status_code}). " + _http_hint(status_code)
        )
    changes = json.loads(_strip_magic(body))
    return [f"{creds.url}/c/{c['_number']}" for c in changes if "_number" in c]


@dataclass
class ChangeListItem:
    number: str
    url: str
    subject: str
    status: str  # NEW / ABANDONED / ...
    project: str
    branch: str
    owner_email: str
    is_owner: bool  # True when the caller owns it (else it's a review request)


def list_my_changes(creds: Credentials) -> list[ChangeListItem]:
    """List the caller's non-merged changes: owned OR assigned for review.

    One server query, ``(owner:self OR reviewer:self) -status:merged``, so
    merged changes are filtered server-side and the union is de-duplicated by
    Gerrit. Each item is tagged ``is_owner`` (the caller pushed it) vs a review
    request (someone else's change assigned to the caller); ownership is decided
    by comparing the change owner's email to the authenticated account's email.
    Read-only — never mutates a change. Used by the CLI's ``--list``.
    """
    auth = creds.auth
    query = quote("(owner:self OR reviewer:self) -status:merged")
    status_code, body = _request(
        "GET",
        f"{creds.url}/a/changes/?q={query}&o=DETAILED_ACCOUNTS",
        auth,
        None,
    )
    if status_code != 200:
        raise VoteError(
            f"Cannot list your changes on {creds.host} (HTTP {status_code}). "
            + _http_hint(status_code)
        )
    changes = json.loads(_strip_magic(body))

    # Resolve the caller's own email once so each change can be tagged owner vs
    # review. The account endpoint reflects the authenticated user (self).
    my_email = _self_email(creds, auth)

    out: list[ChangeListItem] = []
    for c in changes:
        if "_number" not in c:
            continue
        owner_email = (c.get("owner") or {}).get("email", "")
        out.append(
            ChangeListItem(
                number=str(c["_number"]),
                url=f"{creds.url}/c/{c['_number']}",
                subject=c.get("subject", ""),
                status=c.get("status", ""),
                project=c.get("project", ""),
                branch=c.get("branch", ""),
                owner_email=owner_email,
                is_owner=bool(my_email and owner_email.lower() == my_email.lower()),
            )
        )
    return out


def _self_email(creds: Credentials, auth: str) -> str:
    """Return the authenticated account's email, or "" if it can't be read.

    Best-effort: a failure here only means owner/review tagging falls back to
    "review" for everything, which is non-fatal for a listing.
    """
    status_code, body = _request("GET", f"{creds.url}/a/accounts/self", auth, None)
    if status_code != 200:
        return ""
    try:
        return json.loads(_strip_magic(body)).get("email", "") or ""
    except (ValueError, AttributeError):
        return ""


def related_changes(creds: Credentials, change: str) -> list[str]:
    """Return canonical change URLs in ``change``'s dependency (relation) chain.

    Queries the relation chain of ``change``'s current revision. The chain lives
    on a single host, so credentials are the change's own host. The originating
    change is always included (even when it has no relations). Returned
    ``{url}/c/{number}`` URLs feed straight back into vote().
    """
    host, change_id = parse_change(change)
    auth = creds.auth
    status, body = _request(
        "GET",
        f"{creds.url}/a/changes/{change_id}/revisions/current/related",
        auth,
        None,
    )
    if status != 200:
        raise VoteError(
            f"Cannot read relation chain of {change_id} on {creds.host} "
            f"(HTTP {status}). " + _http_hint(status)
        )
    data = json.loads(_strip_magic(body))
    out = [
        f"{creds.url}/c/{c['_change_number']}"
        for c in data.get("changes", [])
        if "_change_number" in c
    ]
    # Ensure the originating change is present even with an empty chain.
    origin = f"{creds.url}/c/{change_id}"
    if origin not in out:
        out.append(origin)
    return out


def change_topic(creds: Credentials, change: str) -> str | None:
    """Return the topic of one change, or None when it has none.

    A minimal GET suffices — ``topic`` is part of Gerrit's basic ChangeInfo
    (no ``o=`` option needed). Used by ``--related-topics`` to discover which
    topics the current vote set carries, before expanding each into its open
    changes. Like related_changes(), the topic lives on a single host, so
    credentials are the change's own host.
    """
    host, change_id = parse_change(change)
    auth = creds.auth
    status, body = _request("GET", f"{creds.url}/a/changes/{change_id}", auth, None)
    if status != 200:
        raise VoteError(
            f"Cannot read change {change_id} on {creds.host} (HTTP {status}). "
            + _http_hint(status)
        )
    return json.loads(_strip_magic(body)).get("topic") or None


def permitted_labels(
    change: str,
    *,
    creds: Credentials | None = None,
    user: str | None = None,
    password: str | None = None,
) -> dict[str, list[str]]:
    """Return {label: [allowed values]} the current user may vote on ``change``.

    This is the authoritative, server-side answer to "what can I vote here" —
    it is per-change and per-user (project/branch/role permissions decide it),
    and differs across hosts, so it is queried live rather than hardcoded. The
    padded zero entry (" 0") is stripped for readability.
    """
    host, change_id = parse_change(change)
    if creds is None:
        creds = resolve(host, user=user, password=password)
    auth = creds.auth
    status, body = _request(
        "GET", f"{creds.url}/a/changes/{change_id}?o=DETAILED_LABELS", auth, None
    )
    if status != 200:
        raise VoteError(
            f"Cannot read change {change_id} on {creds.host} (HTTP {status}). "
            + _http_hint(status)
        )
    info = json.loads(_strip_magic(body))
    permitted = info.get("permitted_labels") or {}
    return {name: [v.strip() for v in values] for name, values in permitted.items()}


# ---------------------------------------------------------------------------
# Change lifecycle: abandon / delete
# ---------------------------------------------------------------------------
#
# These break the module's original "voting only" contract on purpose — the
# tool has grown into a change manager. Both reuse the same GET-then-act shape
# as vote(): read the change, skip closed/ineligible states, honor dry_run, and
# return a VoteResult tagged with the ``action`` so the CLI can render it.


def abandon(
    change: str,
    *,
    message: str | None = None,
    dry_run: bool = False,
    creds: Credentials | None = None,
    user: str | None = None,
    password: str | None = None,
) -> VoteResult:
    """Abandon one change. Reversible (Gerrit keeps a restore action).

    A MERGED change cannot be abandoned, and an already-ABANDONED one is a
    no-op — both are skipped rather than POSTing an error. No owner gate:
    abandoning is reversible and is a legitimate reviewer action, so it is not
    restricted to the caller's own changes.
    """
    host, change_id = parse_change(change)
    if creds is None:
        creds = resolve(host, user=user, password=password)
    auth = creds.auth
    base = creds.url

    status, body = _request(
        "GET",
        f"{base}/a/changes/{change_id}"
        "?o=CURRENT_REVISION&o=CURRENT_COMMIT&o=DETAILED_ACCOUNTS",
        auth,
        None,
    )
    if status != 200:
        raise VoteError(
            f"Cannot read change {change_id} on {creds.host} (HTTP {status}). "
            + _http_hint(status)
        )
    info = json.loads(_strip_magic(body))
    change_status = info.get("status", "")

    def _make(*, skipped: bool, skip_reason: str = "") -> VoteResult:
        return VoteResult(
            change_id=str(info.get("_number", change_id)),
            host=creds.host,
            subject=info.get("subject", ""),
            project=info.get("project", ""),
            branch=info.get("branch", ""),
            status=change_status,
            requested=0,
            cast=None,
            downgraded=False,
            dry_run=dry_run,
            label="",
            skipped=skipped,
            skip_reason=skip_reason,
            owner_email=(info.get("owner") or {}).get("email", ""),
            author_email=_author_email(info),
            action="abandon",
        )

    if change_status == "MERGED":
        return _make(skipped=True, skip_reason="cannot abandon a merged change")
    if change_status == "ABANDONED":
        return _make(skipped=True, skip_reason="already abandoned")

    if dry_run:
        return _make(skipped=False)

    payload: dict = {"message": message} if message else {}
    status, body = _request(
        "POST", f"{base}/a/changes/{change_id}/abandon", auth, payload
    )
    if status != 200:
        raise VoteError(
            f"Abandon failed on {change_id} (HTTP {status}): {_strip_magic(body)[:300]}"
        )
    return _make(skipped=False)


def restore(
    change: str,
    *,
    message: str | None = None,
    dry_run: bool = False,
    creds: Credentials | None = None,
    user: str | None = None,
    password: str | None = None,
) -> VoteResult:
    """Restore one abandoned change. The inverse of abandon().

    Only an ABANDONED change can be restored — a NEW or MERGED one is skipped
    (nothing to restore). Like abandon(), there is no owner gate: restoring is
    reversible and a legitimate reviewer action.
    """
    host, change_id = parse_change(change)
    if creds is None:
        creds = resolve(host, user=user, password=password)
    auth = creds.auth
    base = creds.url

    status, body = _request(
        "GET",
        f"{base}/a/changes/{change_id}"
        "?o=CURRENT_REVISION&o=CURRENT_COMMIT&o=DETAILED_ACCOUNTS",
        auth,
        None,
    )
    if status != 200:
        raise VoteError(
            f"Cannot read change {change_id} on {creds.host} (HTTP {status}). "
            + _http_hint(status)
        )
    info = json.loads(_strip_magic(body))
    change_status = info.get("status", "")

    def _make(*, skipped: bool, skip_reason: str = "") -> VoteResult:
        return VoteResult(
            change_id=str(info.get("_number", change_id)),
            host=creds.host,
            subject=info.get("subject", ""),
            project=info.get("project", ""),
            branch=info.get("branch", ""),
            status=change_status,
            requested=0,
            cast=None,
            downgraded=False,
            dry_run=dry_run,
            label="",
            skipped=skipped,
            skip_reason=skip_reason,
            owner_email=(info.get("owner") or {}).get("email", ""),
            author_email=_author_email(info),
            action="restore",
        )

    # Only an abandoned change can be restored.
    if change_status != "ABANDONED":
        reason = (
            "not abandoned; nothing to restore"
            if change_status
            else "unknown status; nothing to restore"
        )
        return _make(skipped=True, skip_reason=reason)

    if dry_run:
        return _make(skipped=False)

    payload: dict = {"message": message} if message else {}
    status, body = _request(
        "POST", f"{base}/a/changes/{change_id}/restore", auth, payload
    )
    if status != 200:
        raise VoteError(
            f"Restore failed on {change_id} (HTTP {status}): {_strip_magic(body)[:300]}"
        )
    return _make(skipped=False)


def delete_change(
    change: str,
    *,
    require_owner_email: str | None = None,
    dry_run: bool = False,
    creds: Credentials | None = None,
    user: str | None = None,
    password: str | None = None,
) -> VoteResult:
    """Delete one change permanently. IRREVERSIBLE. Never touches a vote.

    Guards, in order:
      * owner gate — when ``require_owner_email`` is given, a change whose owner
        is not that identity is skipped (no request sent). Deleting others'
        changes needs admin-level rights anyway; owner-only keeps the most
        destructive mistake (deleting someone else's work) impossible by design.
      * MERGED — cannot be deleted; skipped.
      * dry_run — returns the plan without sending the DELETE.

    Gerrit's delete endpoint returns 204 No Content on success (not 200).
    """
    host, change_id = parse_change(change)
    if creds is None:
        creds = resolve(host, user=user, password=password)
    auth = creds.auth
    base = creds.url

    status, body = _request(
        "GET",
        f"{base}/a/changes/{change_id}"
        "?o=CURRENT_REVISION&o=CURRENT_COMMIT&o=DETAILED_ACCOUNTS",
        auth,
        None,
    )
    if status != 200:
        raise VoteError(
            f"Cannot read change {change_id} on {creds.host} (HTTP {status}). "
            + _http_hint(status)
        )
    info = json.loads(_strip_magic(body))
    change_status = info.get("status", "")
    owner_email = (info.get("owner") or {}).get("email", "")

    def _make(*, skipped: bool, skip_reason: str = "") -> VoteResult:
        return VoteResult(
            change_id=str(info.get("_number", change_id)),
            host=creds.host,
            subject=info.get("subject", ""),
            project=info.get("project", ""),
            branch=info.get("branch", ""),
            status=change_status,
            requested=0,
            cast=None,
            downgraded=False,
            dry_run=dry_run,
            label="",
            skipped=skipped,
            skip_reason=skip_reason,
            owner_email=owner_email,
            author_email=_author_email(info),
            action="delete",
        )

    # Owner gate: mirrors vote_labels()'s check. Case-insensitive; any missing
    # side is treated as "not owned by you" and skipped.
    if require_owner_email is not None:
        if owner_email.lower() != require_owner_email.lower():
            return _make(
                skipped=True,
                skip_reason=f"not owned by you ({owner_email or 'unknown'}); "
                "not deleted",
            )

    if change_status == "MERGED":
        return _make(skipped=True, skip_reason="cannot delete a merged change")

    if dry_run:
        return _make(skipped=False)

    status, body = _request("DELETE", f"{base}/a/changes/{change_id}", auth, None)
    # Gerrit returns 204 No Content on a successful delete.
    if status not in (200, 204):
        raise VoteError(
            f"Delete failed on {change_id} (HTTP {status}): {_strip_magic(body)[:300]}"
        )
    return _make(skipped=False)
