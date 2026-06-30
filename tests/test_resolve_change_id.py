# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Junbo Zheng. All rights reserved.
"""Tests for /q/I<Change-Id> and bare change-id:<token> resolution (network mocked)."""

import json
from urllib.parse import unquote

import pytest

from gerrit_vote import ConfigError, Credentials, core, resolve_change_ids

CREDS = Credentials(url="https://gerrit.x.example", user="u", password="p")
# Placeholder Change-Id ("I" + 40 hex), NOT copied from a real change.
CID = "I1234567890abcdef1234567890abcdef12345678"
# 8-char short form placeholder, NOT copied from a real change.
CID_SHORT = "I12345678"

CREDS_A = Credentials(url="https://gerrit.a.example", user="u", password="p")
CREDS_B = Credentials(url="https://gerrit.b.example", user="u", password="p")


def _mock_query(monkeypatch, changes, *, status=200):
    """Make the change:<id> query return ``changes`` for every GET."""
    body = ")]}'\n" + json.dumps(changes)

    def fake_request(method, url, auth, payload):
        assert method == "GET"
        return status, body

    monkeypatch.setattr(core, "_request", fake_request)
    monkeypatch.setattr(
        core, "resolve", lambda host, *, user=None, password=None: CREDS
    )


def test_change_id_resolves_to_change_url(monkeypatch):
    _mock_query(monkeypatch, [{"_number": 55001, "status": "NEW"}])
    urls, warnings = resolve_change_ids([f"https://gerrit.x.example/q/{CID}"])
    assert urls == ["https://gerrit.x.example/c/55001"]
    assert warnings == []


def test_plain_change_urls_pass_through(monkeypatch):
    # /c/N URLs carry their own number — no query, no network.
    _mock_query(monkeypatch, [])
    urls, warnings = resolve_change_ids(
        [
            "https://gerrit.x.example/c/p/+/55001",
            "https://gerrit.x.example/c/55002",
        ]
    )
    assert urls == [
        "https://gerrit.x.example/c/p/+/55001",
        "https://gerrit.x.example/c/55002",
    ]
    assert warnings == []


def test_change_id_no_open_match_warns_and_skips(monkeypatch):
    _mock_query(monkeypatch, [])
    urls, warnings = resolve_change_ids([f"https://gerrit.x.example/q/{CID}"])
    assert urls == []
    assert len(warnings) == 1
    assert "already merged" in warnings[0]


def test_change_id_multiple_open_matches_votes_all(monkeypatch):
    # Cross-branch cherry-pick: one Change-Id, two open changes (one per
    # branch). All are voted — mirrors topic/related expansion, so a
    # cross-branch patch is voted on every branch consistently.
    _mock_query(
        monkeypatch,
        [
            {"_number": 55001, "status": "NEW"},
            {"_number": 55002, "status": "NEW"},
        ],
    )
    urls, warnings = resolve_change_ids([f"https://gerrit.x.example/q/{CID}"])
    assert urls == [
        "https://gerrit.x.example/c/55001",
        "https://gerrit.x.example/c/55002",
    ]
    assert len(warnings) == 1
    assert "resolved to 2 open changes" in warnings[0]
    assert "55001" in warnings[0] and "55002" in warnings[0]


def test_change_id_http_error_raises(monkeypatch):
    _mock_query(monkeypatch, [], status=500)
    with pytest.raises(core.VoteError):
        resolve_change_ids([f"https://gerrit.x.example/q/{CID}"])


def test_resolved_change_id_dedups_against_existing_change_url(monkeypatch):
    # A /q/I... resolving to a change already in the batch as /c/N collapses
    # to one entry (first-seen form kept), so it is not voted twice.
    _mock_query(monkeypatch, [{"_number": 55001, "status": "NEW"}])
    urls, _ = resolve_change_ids(
        [
            "https://gerrit.x.example/c/p/+/55001",
            f"https://gerrit.x.example/q/{CID}",
        ]
    )
    assert urls == ["https://gerrit.x.example/c/p/+/55001"]


# ---------------------------------------------------------------------------
# Bare change-id:<token> carrier resolution (no host → query all hosts)
# ---------------------------------------------------------------------------


def _mock_multi_host_query(monkeypatch, host_to_changes, *, status=200):
    """Make change:<id> return per-host results, keyed by host substring in url."""

    def fake_request(method, url, auth, payload):
        assert method == "GET"
        for host, changes in host_to_changes.items():
            if host in url:
                return status, ")]}'\n" + json.dumps(changes)
        return status, ")]}'\n" + json.dumps([])

    monkeypatch.setattr(core, "_request", fake_request)


def _stub_all_hosts(monkeypatch, creds_list):
    """Stub all_hosts() to return a fixed list of Credentials."""
    monkeypatch.setattr(
        core, "all_hosts", lambda *, user=None, password=None: creds_list
    )


def test_bare_change_id_resolves_on_single_host(monkeypatch):
    _mock_multi_host_query(
        monkeypatch, {"gerrit.x.example": [{"_number": 55001, "status": "NEW"}]}
    )
    _stub_all_hosts(monkeypatch, [CREDS])
    urls, warnings = resolve_change_ids([f"change-id:{CID_SHORT}"])
    assert urls == ["https://gerrit.x.example/c/55001"]
    assert warnings == []


def test_bare_change_id_resolves_on_multiple_hosts(monkeypatch):
    _mock_multi_host_query(
        monkeypatch,
        {
            "gerrit.a.example": [{"_number": 55001, "status": "NEW"}],
            "gerrit.b.example": [{"_number": 55002, "status": "NEW"}],
        },
    )
    _stub_all_hosts(monkeypatch, [CREDS_A, CREDS_B])
    urls, warnings = resolve_change_ids([f"change-id:{CID_SHORT}"])
    assert urls == [
        "https://gerrit.a.example/c/55001",
        "https://gerrit.b.example/c/55002",
    ]
    assert len(warnings) == 1
    assert "matched on 2 hosts" in warnings[0]


def test_bare_change_id_no_match_on_any_host(monkeypatch):
    _mock_multi_host_query(
        monkeypatch, {"gerrit.a.example": [], "gerrit.b.example": []}
    )
    _stub_all_hosts(monkeypatch, [CREDS_A, CREDS_B])
    urls, warnings = resolve_change_ids([f"change-id:{CID_SHORT}"])
    assert urls == []
    assert len(warnings) == 1
    assert "already merged" in warnings[0]
    assert "gerrit.a.example" in warnings[0] and "gerrit.b.example" in warnings[0]


def test_bare_change_id_short_form_query_passed_as_is(monkeypatch):
    # The 8-hex token is passed verbatim to change:<token> (no expansion).
    captured = {}

    def fake_request(method, url, auth, payload):
        assert method == "GET"
        captured["url"] = url
        return 200, ")]}'\n" + json.dumps([{"_number": 55001, "status": "NEW"}])

    monkeypatch.setattr(core, "_request", fake_request)
    _stub_all_hosts(monkeypatch, [CREDS])
    resolve_change_ids([f"change-id:{CID_SHORT}"])
    decoded = unquote(captured["url"])
    assert f"change:{CID_SHORT}" in decoded
    assert "status:open" in decoded


def test_bare_change_id_multiple_matches_on_one_host(monkeypatch):
    _mock_multi_host_query(
        monkeypatch,
        {
            "gerrit.a.example": [
                {"_number": 55001, "status": "NEW"},
                {"_number": 55002, "status": "NEW"},
            ]
        },
    )
    _stub_all_hosts(monkeypatch, [CREDS_A])
    urls, warnings = resolve_change_ids([f"change-id:{CID_SHORT}"])
    assert urls == [
        "https://gerrit.a.example/c/55001",
        "https://gerrit.a.example/c/55002",
    ]
    assert len(warnings) == 1
    assert "resolved to 2 open changes" in warnings[0]


def test_bare_change_id_carrier_and_url_dedup(monkeypatch):
    # A bare carrier and a /q/I<id> URL for the SAME change collapse to one
    # /c/N after resolution (resolve_change_ids dedups its output).
    _mock_multi_host_query(
        monkeypatch, {"gerrit.x.example": [{"_number": 55001, "status": "NEW"}]}
    )
    _stub_all_hosts(monkeypatch, [CREDS])
    monkeypatch.setattr(
        core, "resolve", lambda host, *, user=None, password=None: CREDS
    )
    urls, _ = resolve_change_ids(
        [f"change-id:{CID}", f"https://gerrit.x.example/q/{CID}"]
    )
    assert urls == ["https://gerrit.x.example/c/55001"]


def test_bare_change_id_http_error_raises(monkeypatch):
    _mock_multi_host_query(monkeypatch, {"gerrit.x.example": []}, status=500)
    _stub_all_hosts(monkeypatch, [CREDS])
    with pytest.raises(core.VoteError):
        resolve_change_ids([f"change-id:{CID_SHORT}"])


def test_bare_change_id_no_hosts_configured_raises(monkeypatch):
    def _raise(*, user=None, password=None):
        raise ConfigError("no hosts configured")

    monkeypatch.setattr(core, "all_hosts", _raise)
    with pytest.raises(ConfigError):
        resolve_change_ids([f"change-id:{CID_SHORT}"])
