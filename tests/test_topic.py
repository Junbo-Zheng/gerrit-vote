# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Junbo Zheng. All rights reserved.
"""Tests for topic parsing, extraction, and topic-to-changes querying."""

import json

import pytest

from gerrit_vote import (
    Credentials,
    VoteError,
    change_topic,
    changes_in_topic,
    core,
    extract_topics,
    parse_topic,
    related_changes,
)

CREDS = Credentials(url="https://gerrit.x.example", user="u", password="p")


# ---------------------------------------------------------------------------
# parse_topic
# ---------------------------------------------------------------------------


def test_parse_topic_canonical():
    assert parse_topic("https://gerrit.odm.example/q/topic:dnd_sync_revert") == (
        "gerrit.odm.example",
        None,
        "dnd_sync_revert",
    )


def test_parse_topic_fragment_form():
    assert parse_topic("https://gerrit.x.example/#/q/topic:foo") == (
        "gerrit.x.example",
        None,
        "foo",
    )


def test_parse_topic_quoted_and_encoded():
    assert parse_topic('https://gerrit.x.example/q/topic:"my feature"') == (
        "gerrit.x.example",
        None,
        "my feature",
    )
    assert parse_topic("https://gerrit.x.example/q/topic:%22enc%22") == (
        "gerrit.x.example",
        None,
        "enc",
    )


def test_parse_topic_trailing_cjk_punctuation():
    assert parse_topic("https://gerrit.x.example/q/topic:foo。") == (
        "gerrit.x.example",
        None,
        "foo",
    )


def test_parse_topic_with_trailing_filters():
    # A canonical topic URL with extra query filters must keep only the topic
    # name and the branch scope, not swallow "+branch:..." into the name.
    assert parse_topic(
        "https://gerrit.x.example/q/topic:foo+branch:dev+status:open"
    ) == ("gerrit.x.example", "dev", "foo")


def test_parse_topic_bare_name_search_box_form():
    # The search-box form Gerrit emits for a saved topic filter: the topic name
    # is the bare first term (no "topic:" prefix), then field:value filters.
    url = "https://gerrit.odm.example/q/media-control-fix+branch:stable-4.0+status:open"
    assert parse_topic(url) == (
        "gerrit.odm.example",
        "stable-4.0",
        "media-control-fix",
    )


def test_parse_topic_rejects_non_topic_and_non_gerrit():
    assert parse_topic("https://gerrit.x.example/c/p/+/123") is None
    assert parse_topic("https://example.com/q/topic:foo") is None  # not gerrit
    assert parse_topic("12345") is None  # bare number, not a URL
    # A bare word with no trailing filter is a full-text search, not a topic.
    assert parse_topic("https://gerrit.x.example/q/just_a_word") is None


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
        ("gerrit.odm.example", None, "dnd_sync_revert"),
        ("gerrit.pt.example", None, "foo"),
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
        ("gerrit.odm.example", None, "my-revert-topic"),
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
        ("gerrit.x.example", None, "my-sleep-topic"),
    ]


def test_extract_topics_search_box_form_with_branch():
    text = (
        "see https://gerrit.odm.example/q/"
        "media-control-fix+branch:stable-4.0+status:open"
    )
    assert extract_topics(text) == [
        (
            "gerrit.odm.example",
            "stable-4.0",
            "media-control-fix",
        ),
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

    monkeypatch.setattr(core, "_request", fake_request)
    return captured


def test_changes_in_topic_returns_canonical_urls(monkeypatch):
    captured = _mock_query(
        monkeypatch,
        [
            {"_number": 100, "subject": "a", "status": "NEW"},
            {"_number": 101, "subject": "b", "status": "NEW"},
        ],
    )
    got = changes_in_topic(CREDS, "my-topic")
    assert got == [
        ("https://gerrit.x.example/c/100", "NEW"),
        ("https://gerrit.x.example/c/101", "NEW"),
    ]
    # Query is server-side filtered to open changes (URL-encoded in the path).
    from urllib.parse import unquote

    assert captured["method"] == "GET"
    decoded = unquote(captured["url"])
    assert "status:open" in decoded
    assert 'topic:"my-topic"' in decoded


def test_changes_in_topic_status_none_omits_filter(monkeypatch):
    captured = _mock_query(monkeypatch, [{"_number": 200, "status": "MERGED"}])
    got = changes_in_topic(CREDS, "my-topic", status=None)
    assert got == [("https://gerrit.x.example/c/200", "MERGED")]
    from urllib.parse import unquote

    decoded = unquote(captured["url"])
    assert "status:" not in decoded
    assert 'topic:"my-topic"' in decoded


def test_changes_in_topic_scopes_to_branch(monkeypatch):
    captured = _mock_query(monkeypatch, [{"_number": 300, "subject": "x"}])
    urls = changes_in_topic(CREDS, "my-topic", branch="stable-4.0")
    assert urls == [("https://gerrit.x.example/c/300", "?")]
    from urllib.parse import unquote

    decoded = unquote(captured["url"])
    assert 'topic:"my-topic"' in decoded
    assert 'branch:"stable-4.0"' in decoded
    assert "status:open" in decoded


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

    monkeypatch.setattr(core, "_request", fake_request)


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


# ---------------------------------------------------------------------------
# change_topic
# ---------------------------------------------------------------------------


def _mock_change(monkeypatch, info, status=200):
    body = ")]}'\n" + json.dumps(info)

    def fake_request(method, url, auth, payload):
        return status, body

    monkeypatch.setattr(core, "_request", fake_request)


def test_change_topic_returns_topic(monkeypatch):
    _mock_change(monkeypatch, {"_number": 500, "topic": "my-feature"})
    assert change_topic(CREDS, "https://gerrit.x.example/c/500") == "my-feature"


def test_change_topic_none_when_absent(monkeypatch):
    # No topic field (or null) -> None, so --related-topics skips this change.
    _mock_change(monkeypatch, {"_number": 501})
    assert change_topic(CREDS, "https://gerrit.x.example/c/501") is None
    _mock_change(monkeypatch, {"_number": 501, "topic": None})
    assert change_topic(CREDS, "https://gerrit.x.example/c/501") is None


def test_change_topic_http_error(monkeypatch):
    _mock_change(monkeypatch, {}, status=403)
    with pytest.raises(VoteError):
        change_topic(CREDS, "https://gerrit.x.example/c/502")
