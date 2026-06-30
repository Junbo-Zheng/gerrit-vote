# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Junbo Zheng. All rights reserved.
"""Tests for topic parsing, extraction, and topic-to-changes querying."""

import json

import pytest

from gerrit_vote import (
    Credentials,
    VoteError,
    changes_in_topic,
    extract_topics,
    parse_topic,
    related_changes,
)
from gerrit_vote import _core

CREDS = Credentials(url="https://gerrit.x.example", user="u", password="p")


# ---------------------------------------------------------------------------
# parse_topic
# ---------------------------------------------------------------------------


def test_parse_topic_canonical():
    assert parse_topic("https://gerrit.odm.example/q/topic:dnd_sync_revert") == (
        "gerrit.odm.example",
        "dnd_sync_revert",
    )


def test_parse_topic_fragment_form():
    assert parse_topic("https://gerrit.x.example/#/q/topic:foo") == (
        "gerrit.x.example",
        "foo",
    )


def test_parse_topic_quoted_and_encoded():
    assert parse_topic('https://gerrit.x.example/q/topic:"my feature"') == (
        "gerrit.x.example",
        "my feature",
    )
    assert parse_topic("https://gerrit.x.example/q/topic:%22enc%22") == (
        "gerrit.x.example",
        "enc",
    )


def test_parse_topic_trailing_cjk_punctuation():
    assert parse_topic("https://gerrit.x.example/q/topic:foo。") == (
        "gerrit.x.example",
        "foo",
    )


def test_parse_topic_rejects_non_topic_and_non_gerrit():
    assert parse_topic("https://gerrit.x.example/c/p/+/123") is None
    assert parse_topic("https://example.com/q/topic:foo") is None  # not gerrit
    assert parse_topic("12345") is None  # bare number, not a URL


# ---------------------------------------------------------------------------
# extract_topics
# ---------------------------------------------------------------------------


def test_extract_topics_dedup_first_seen():
    text = """
    revert batch https://gerrit.odm.example/q/topic:dnd_sync_revert and
    other https://gerrit.pt.example/q/topic:foo
    dup https://gerrit.odm.example/q/topic:dnd_sync_revert
    a normal change https://gerrit.pt.example/c/p/+/999 is ignored here
    """
    assert extract_topics(text) == [
        ("gerrit.odm.example", "dnd_sync_revert"),
        ("gerrit.pt.example", "foo"),
    ]


def test_extract_topics_comma_delimited_csv_row():
    # CSV/table paste: a "/q/topic:NAME" URL followed by ",col,col,...". The
    # comma must terminate the token, else the trailing columns are swallowed
    # into the topic name and the server query 400s.
    text = (
        "bug,TASK-1,https://gerrit.odm.example/q/topic:my-revert-topic,"
        "board,branch,col,name\n"
    )
    assert extract_topics(text) == [
        ("gerrit.odm.example", "my-revert-topic"),
    ]


def test_extract_topics_tab_delimited_excel_with_multiline_cell():
    # Excel/spreadsheet paste: TAB-separated columns, and a quoted MULTILINE
    # cell (embedded newlines). A "/q/topic:NAME" URL followed by a TAB must
    # yield the bare topic name, undisturbed by the row layout or the multiline
    # cell that follows on a later physical line.
    text = (
        "sleep\thttps://gerrit.x.example/q/topic:my-sleep-topic\tdone\tapp\t"
        'TASK-1\t"1) foo changed A to B\n'
        "2) bar does not return\n"
        '3) queue size set to 5"\tcol\tPass\n'
    )
    assert extract_topics(text) == [
        ("gerrit.x.example", "my-sleep-topic"),
    ]


# ---------------------------------------------------------------------------
# changes_in_topic
# ---------------------------------------------------------------------------


def _mock_query(monkeypatch, change_list, status=200):
    body = ")]}'\n" + json.dumps(change_list)
    captured = {}

    def fake_request(method, url, auth, payload):
        captured["method"] = method
        captured["url"] = url
        return status, body

    monkeypatch.setattr(_core, "_request", fake_request)
    return captured


def test_changes_in_topic_returns_canonical_urls(monkeypatch):
    captured = _mock_query(
        monkeypatch,
        [{"_number": 100, "subject": "a"}, {"_number": 101, "subject": "b"}],
    )
    urls = changes_in_topic(CREDS, "my-topic")
    assert urls == [
        "https://gerrit.x.example/c/100",
        "https://gerrit.x.example/c/101",
    ]
    # Query is server-side filtered to open changes (URL-encoded in the path).
    from urllib.parse import unquote

    assert captured["method"] == "GET"
    decoded = unquote(captured["url"])
    assert "status:open" in decoded
    assert 'topic:"my-topic"' in decoded


def test_changes_in_topic_empty(monkeypatch):
    _mock_query(monkeypatch, [])
    assert changes_in_topic(CREDS, "nothing-open") == []


def test_changes_in_topic_http_error(monkeypatch):
    _mock_query(monkeypatch, [], status=403)
    with pytest.raises(VoteError):
        changes_in_topic(CREDS, "denied")


# ---------------------------------------------------------------------------
# related_changes
# ---------------------------------------------------------------------------


def _mock_related(monkeypatch, payload, status=200):
    body = ")]}'\n" + json.dumps(payload)

    def fake_request(method, url, auth, payload_arg):
        return status, body

    monkeypatch.setattr(_core, "_request", fake_request)


def test_related_changes_returns_chain_with_origin(monkeypatch):
    _mock_related(
        monkeypatch,
        {"changes": [{"_change_number": 200}, {"_change_number": 201}]},
    )
    urls = related_changes(CREDS, "https://gerrit.x.example/c/200")
    assert urls == [
        "https://gerrit.x.example/c/200",
        "https://gerrit.x.example/c/201",
    ]


def test_related_changes_empty_chain_keeps_origin(monkeypatch):
    # No relation chain -> still returns the originating change alone.
    _mock_related(monkeypatch, {"changes": []})
    assert related_changes(CREDS, "https://gerrit.x.example/c/300") == [
        "https://gerrit.x.example/c/300",
    ]


def test_related_changes_http_error(monkeypatch):
    _mock_related(monkeypatch, {}, status=403)
    with pytest.raises(VoteError):
        related_changes(CREDS, "https://gerrit.x.example/c/400")
