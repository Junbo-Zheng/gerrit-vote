# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Junbo Zheng. All rights reserved.
"""Tests for /q/I<Change-Id> -> /c/<number> resolution (network mocked)."""

import json

import pytest

from gerrit_vote import Credentials, core, resolve_change_ids

CREDS = Credentials(url="https://gerrit.x.example", user="u", password="p")
# Placeholder Change-Id ("I" + 40 hex), NOT copied from a real change.
CID = "I1234567890abcdef1234567890abcdef12345678"


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
    assert "no open change matched" in warnings[0]


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
