# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Junbo Zheng. All rights reserved.
"""Tests for vote() outcome logic (network mocked)."""

import json

import pytest

from gerrit_vote import Credentials, vote
from gerrit_vote import _core

CREDS = Credentials(url="https://gerrit.x.example", user="u", password="p")


def _mock_get(monkeypatch, change_info):
    """Make _request return change_info for GET, succeed for POST."""
    body = ")]}'\n" + json.dumps(change_info)

    def fake_request(method, url, auth, payload):
        if method == "GET":
            return 200, body
        return 200, ")]}'\n{}"

    monkeypatch.setattr(_core, "_request", fake_request)


def test_merged_change_is_skipped(monkeypatch):
    _mock_get(monkeypatch, {"_number": 100, "status": "MERGED", "subject": "x"})
    r = vote("https://gerrit.x.example/c/p/+/100", 2, creds=CREDS)
    assert r.skipped is True
    assert r.cast is None
    assert "merged" in r.skip_reason


def test_abandoned_change_is_skipped(monkeypatch):
    _mock_get(monkeypatch, {"_number": 101, "status": "ABANDONED", "subject": "x"})
    r = vote("https://gerrit.x.example/c/p/+/101", 2, creds=CREDS)
    assert r.skipped is True
    assert "abandoned" in r.skip_reason


def test_new_change_votes_plus2(monkeypatch):
    _mock_get(
        monkeypatch,
        {
            "_number": 102,
            "status": "NEW",
            "subject": "x",
            "permitted_labels": {"Code-Review": ["-2", "-1", "0", "+1", "+2"]},
        },
    )
    r = vote("https://gerrit.x.example/c/p/+/102", 2, creds=CREDS)
    assert r.skipped is False
    assert r.cast == 2
    assert r.downgraded is False


def test_plus2_downgrades_to_plus1_when_not_permitted(monkeypatch):
    _mock_get(
        monkeypatch,
        {
            "_number": 103,
            "status": "NEW",
            "subject": "x",
            "permitted_labels": {"Code-Review": ["-1", "0", "+1"]},
        },
    )
    r = vote("https://gerrit.x.example/c/p/+/103", 2, creds=CREDS)
    assert r.cast == 1
    assert r.downgraded is True


def test_zero_vote_matches_space_padded_permitted(monkeypatch):
    # Gerrit pads the zero entry to " 0" for alignment with "-1"/"+1"; the
    # permission check must strip padding so fmt(0) == "0" still matches.
    _mock_get(
        monkeypatch,
        {
            "_number": 106,
            "status": "NEW",
            "subject": "x",
            "permitted_labels": {"Code-Review": ["-2", "-1", " 0", "+1", "+2"]},
        },
    )
    r = vote("https://gerrit.x.example/c/p/+/106", 0, creds=CREDS)
    assert r.skipped is False
    assert r.cast == 0
    assert r.downgraded is False


def test_network_error_raises_voteerror(monkeypatch):
    import urllib.error

    from gerrit_vote import VoteError

    calls = {"n": 0}

    def boom(req, timeout):
        calls["n"] += 1
        raise urllib.error.URLError("Connection refused")

    monkeypatch.setattr(_core.urllib.request, "urlopen", boom)
    monkeypatch.setattr(_core.time, "sleep", lambda s: None)  # no real backoff
    with pytest.raises(VoteError, match="network error"):
        vote("https://gerrit.x.example/c/p/+/104", 2, creds=CREDS)
    # A connection-level error is transient -> retried (1 initial + _RETRIES).
    assert calls["n"] == _core._RETRIES + 1


def test_request_retries_5xx_then_succeeds(monkeypatch):
    # A 500 is transient: the request retries and succeeds on a later attempt.
    import urllib.error

    seq = [500, 500, 200]  # fail, fail, then ok
    calls = {"n": 0}

    def flaky(req, timeout):
        code = seq[calls["n"]]
        calls["n"] += 1
        if code >= 500:
            raise urllib.error.HTTPError(req.full_url, code, "err", {}, None)

        class _Resp:
            status = 200

            def read(self):
                return b")]}'\n{}"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return _Resp()

    monkeypatch.setattr(_core.urllib.request, "urlopen", flaky)
    monkeypatch.setattr(_core.time, "sleep", lambda s: None)
    status, _body = _core._request("GET", "https://gerrit.x.example/a/x", "auth", None)
    assert status == 200
    assert calls["n"] == 3  # two 5xx retries + one success


def test_request_does_not_retry_4xx(monkeypatch):
    # A 404 is deterministic: returned immediately, no retry.
    import urllib.error

    calls = {"n": 0}

    def not_found(req, timeout):
        calls["n"] += 1
        raise urllib.error.HTTPError(req.full_url, 404, "nope", {}, None)

    monkeypatch.setattr(_core.urllib.request, "urlopen", not_found)
    monkeypatch.setattr(_core.time, "sleep", lambda s: None)
    status, _body = _core._request("GET", "https://gerrit.x.example/a/x", "auth", None)
    assert status == 404
    assert calls["n"] == 1  # no retry on 4xx


def test_unreadable_change_raises_voteerror(monkeypatch):
    # GET returns a non-200 (e.g. 404/expired creds) -> clean VoteError.
    def fake_request(method, url, auth, payload):
        return 404, ")]}'\n{}"

    monkeypatch.setattr(_core, "_request", fake_request)
    from gerrit_vote import VoteError

    with pytest.raises(VoteError, match="Cannot read change"):
        vote("https://gerrit.x.example/c/p/+/105", 2, creds=CREDS)


def test_error_hint_maps_status_to_cause(monkeypatch):
    # The failure hint must reflect the HTTP code, not always blame creds:
    #   404 -> change does not exist (NOT "credentials expired")
    #   401 -> credentials expired
    #   403 -> no permission
    from gerrit_vote import VoteError

    for code, needle, antineedle in [
        (404, "does not exist", "Credentials expired"),
        (401, "Credentials expired", "does not exist"),
        (403, "No permission", "does not exist"),
    ]:
        monkeypatch.setattr(_core, "_request", lambda *a, c=code: (c, ")]}'\n{}"))
        with pytest.raises(VoteError) as exc:
            vote("https://gerrit.x.example/c/p/+/1", 2, creds=CREDS)
        msg = str(exc.value)
        assert needle in msg
        assert antineedle not in msg


def test_vote_result_carries_label(monkeypatch):
    # A non-default label flows through to VoteResult so the display layer
    # (CLI) can show the actual label instead of a hardcoded "Code-Review".
    _mock_get(
        monkeypatch,
        {
            "_number": 107,
            "status": "NEW",
            "subject": "x",
            "permitted_labels": {"Verified": ["-1", " 0", "+1"]},
        },
    )
    r = vote("https://gerrit.x.example/c/p/+/107", 1, label="Verified", creds=CREDS)
    assert r.label == "Verified"
    assert r.cast == 1


def test_permitted_labels_returns_stripped_map(monkeypatch):
    from gerrit_vote import permitted_labels

    _mock_get(
        monkeypatch,
        {
            "_number": 108,
            "status": "NEW",
            "permitted_labels": {
                "Code-Review": ["-2", "-1", " 0", "+1", "+2"],
                "Verified": ["-1", " 0", "+1"],
            },
        },
    )
    labels = permitted_labels("https://gerrit.x.example/c/p/+/108", creds=CREDS)
    assert labels == {
        "Code-Review": ["-2", "-1", "0", "+1", "+2"],
        "Verified": ["-1", "0", "+1"],
    }


def _mock_get_capture(monkeypatch, change_info):
    """Like _mock_get but records the POST payload for assertions.

    Returns a dict whose "payload" key is set to the POSTed body (or stays
    absent when no POST is made, e.g. a skip).
    """
    body = ")]}'\n" + json.dumps(change_info)
    captured: dict = {}

    def fake_request(method, url, auth, payload):
        if method == "GET":
            return 200, body
        captured["payload"] = payload
        return 200, ")]}'\n{}"

    monkeypatch.setattr(_core, "_request", fake_request)
    return captured


_COMBO = {"Code-Review": 1, "Presubmit-Ready": 1, "Auto-Submit": 1}
_OWNED = {
    "_number": 200,
    "status": "NEW",
    "subject": "x",
    "owner": {"email": "me@example.com"},
    "permitted_labels": {
        "Code-Review": ["+1", "+2"],
        "Presubmit-Ready": [" 0", "+1"],
        "Auto-Submit": [" 0", "+1"],
    },
}


def test_vote_labels_atomic_posts_all_labels(monkeypatch):
    from gerrit_vote import vote_labels

    cap = _mock_get_capture(monkeypatch, _OWNED)
    r = vote_labels(
        "https://gerrit.x.example/c/p/+/200",
        _COMBO,
        require_owner_email="me@example.com",
        creds=CREDS,
    )
    assert r.skipped is False
    assert r.labels == _COMBO
    assert cap["payload"]["labels"] == _COMBO  # one POST, all three labels
    assert r.owner_email == "me@example.com"


def test_vote_labels_owner_gate_case_insensitive(monkeypatch):
    from gerrit_vote import vote_labels

    cap = _mock_get_capture(monkeypatch, _OWNED)
    r = vote_labels(
        "https://gerrit.x.example/c/p/+/200",
        _COMBO,
        require_owner_email="ME@EXAMPLE.COM",  # different case, same identity
        creds=CREDS,
    )
    assert r.skipped is False
    assert cap["payload"]["labels"] == _COMBO


def test_vote_labels_skips_when_not_owner(monkeypatch):
    from gerrit_vote import vote_labels

    cap = _mock_get_capture(monkeypatch, _OWNED)
    r = vote_labels(
        "https://gerrit.x.example/c/p/+/200",
        _COMBO,
        require_owner_email="someone-else@example.com",
        creds=CREDS,
    )
    assert r.skipped is True
    assert "not owned by you" in r.skip_reason
    assert "payload" not in cap  # nothing POSTed


def test_vote_labels_skips_when_a_label_not_permitted(monkeypatch):
    from gerrit_vote import vote_labels

    info = dict(_OWNED)
    info["permitted_labels"] = {"Code-Review": ["+1"], "Presubmit-Ready": ["+1"]}
    cap = _mock_get_capture(monkeypatch, info)
    r = vote_labels(
        "https://gerrit.x.example/c/p/+/200",
        _COMBO,  # Auto-Submit not permitted -> whole combo skipped
        require_owner_email="me@example.com",
        creds=CREDS,
    )
    assert r.skipped is True
    assert "Auto-Submit" in r.skip_reason
    assert "payload" not in cap


def test_vote_labels_skips_merged_change(monkeypatch):
    from gerrit_vote import vote_labels

    cap = _mock_get_capture(
        monkeypatch, {"_number": 201, "status": "MERGED", "subject": "x"}
    )
    r = vote_labels(
        "https://gerrit.x.example/c/p/+/201",
        _COMBO,
        require_owner_email="me@example.com",
        creds=CREDS,
    )
    assert r.skipped is True
    assert "merged" in r.skip_reason
    assert "payload" not in cap


def test_vote_populates_owner_email(monkeypatch):
    _mock_get(
        monkeypatch,
        {
            "_number": 202,
            "status": "NEW",
            "subject": "x",
            "owner": {"email": "author@example.com"},
            "permitted_labels": {"Code-Review": ["-2", "-1", "0", "+1", "+2"]},
        },
    )
    r = vote("https://gerrit.x.example/c/p/+/202", 2, creds=CREDS)
    assert r.owner_email == "author@example.com"
