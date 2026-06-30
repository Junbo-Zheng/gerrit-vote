# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Junbo Zheng. All rights reserved.
"""Tests for abandon() and delete_change() (network fully mocked).

Every test monkeypatches core._request, so NO real Gerrit request is ever
made and nothing is ever really abandoned or deleted — mirroring the mock
pattern in test_vote.py. Assertions target the method / URL / payload that
WOULD be sent, plus the skip/dry-run guards.
"""

import json

import pytest

from gerrit_vote import Credentials, abandon, core, delete_change

CREDS = Credentials(url="https://gerrit.x.example", user="u", password="p")


def _mock(monkeypatch, change_info, *, act_status=200):
    """Return a dict capturing the non-GET request (method/url/payload).

    GET returns change_info; the action request (POST abandon / DELETE) returns
    ``act_status`` with an empty magic-prefixed body.
    """
    body = ")]}'\n" + json.dumps(change_info)
    cap: dict = {}

    def fake_request(method, url, auth, payload):
        if method == "GET":
            return 200, body
        cap["method"] = method
        cap["url"] = url
        cap["payload"] = payload
        return act_status, ")]}'\n{}"

    monkeypatch.setattr(core, "_request", fake_request)
    return cap


_NEW = {
    "_number": 300,
    "status": "NEW",
    "subject": "x",
    "owner": {"email": "me@example.com"},
}


# --------------------------------------------------------------------------
# abandon
# --------------------------------------------------------------------------


def test_abandon_new_change_posts_to_abandon(monkeypatch):
    cap = _mock(monkeypatch, _NEW)
    r = abandon("https://gerrit.x.example/c/p/+/300", creds=CREDS)
    assert r.skipped is False
    assert r.action == "abandon"
    assert cap["method"] == "POST"
    assert cap["url"].endswith("/a/changes/300/abandon")
    assert cap["payload"] == {}  # no message


def test_abandon_with_message_includes_it(monkeypatch):
    cap = _mock(monkeypatch, _NEW)
    r = abandon("https://gerrit.x.example/c/p/+/300", message="stale", creds=CREDS)
    assert r.skipped is False
    assert cap["payload"] == {"message": "stale"}


def test_abandon_merged_is_skipped(monkeypatch):
    cap = _mock(monkeypatch, {"_number": 301, "status": "MERGED", "subject": "x"})
    r = abandon("https://gerrit.x.example/c/p/+/301", creds=CREDS)
    assert r.skipped is True
    assert "merged" in r.skip_reason
    assert "method" not in cap  # nothing POSTed


def test_abandon_already_abandoned_is_skipped(monkeypatch):
    cap = _mock(monkeypatch, {"_number": 302, "status": "ABANDONED", "subject": "x"})
    r = abandon("https://gerrit.x.example/c/p/+/302", creds=CREDS)
    assert r.skipped is True
    assert "already abandoned" in r.skip_reason
    assert "method" not in cap


def test_abandon_dry_run_sends_nothing(monkeypatch):
    cap = _mock(monkeypatch, _NEW)
    r = abandon("https://gerrit.x.example/c/p/+/300", dry_run=True, creds=CREDS)
    assert r.dry_run is True
    assert r.skipped is False
    assert "method" not in cap  # dry-run: no POST


def test_abandon_unreadable_change_raises(monkeypatch):
    from gerrit_vote import VoteError

    def fake_request(method, url, auth, payload):
        return 404, ")]}'\n{}"

    monkeypatch.setattr(core, "_request", fake_request)
    with pytest.raises(VoteError, match="Cannot read change"):
        abandon("https://gerrit.x.example/c/p/+/999", creds=CREDS)


# --------------------------------------------------------------------------
# restore — inverse of abandon
# --------------------------------------------------------------------------

_ABANDONED = {
    "_number": 400,
    "status": "ABANDONED",
    "subject": "z",
    "owner": {"email": "me@example.com"},
}


def test_restore_abandoned_posts_to_restore(monkeypatch):
    from gerrit_vote import restore

    cap = _mock(monkeypatch, _ABANDONED)
    r = restore("https://gerrit.x.example/c/p/+/400", creds=CREDS)
    assert r.skipped is False
    assert r.action == "restore"
    assert cap["method"] == "POST"
    assert cap["url"].endswith("/a/changes/400/restore")
    assert cap["payload"] == {}


def test_restore_with_message(monkeypatch):
    from gerrit_vote import restore

    cap = _mock(monkeypatch, _ABANDONED)
    r = restore("https://gerrit.x.example/c/p/+/400", message="back", creds=CREDS)
    assert r.skipped is False
    assert cap["payload"] == {"message": "back"}


def test_restore_new_change_is_skipped(monkeypatch):
    from gerrit_vote import restore

    cap = _mock(monkeypatch, _NEW)  # NEW, not abandoned
    r = restore("https://gerrit.x.example/c/p/+/300", creds=CREDS)
    assert r.skipped is True
    assert "not abandoned" in r.skip_reason
    assert "method" not in cap  # nothing POSTed


def test_restore_merged_is_skipped(monkeypatch):
    from gerrit_vote import restore

    cap = _mock(monkeypatch, {"_number": 401, "status": "MERGED", "subject": "z"})
    r = restore("https://gerrit.x.example/c/p/+/401", creds=CREDS)
    assert r.skipped is True
    assert "not abandoned" in r.skip_reason
    assert "method" not in cap


def test_restore_dry_run_sends_nothing(monkeypatch):
    from gerrit_vote import restore

    cap = _mock(monkeypatch, _ABANDONED)
    r = restore("https://gerrit.x.example/c/p/+/400", dry_run=True, creds=CREDS)
    assert r.dry_run is True
    assert r.skipped is False
    assert "method" not in cap


# --------------------------------------------------------------------------
# delete_change
# --------------------------------------------------------------------------


def test_delete_owner_match_sends_delete(monkeypatch):
    cap = _mock(monkeypatch, _NEW, act_status=204)  # Gerrit returns 204
    r = delete_change(
        "https://gerrit.x.example/c/p/+/300",
        require_owner_email="me@example.com",
        creds=CREDS,
    )
    assert r.skipped is False
    assert r.action == "delete"
    assert cap["method"] == "DELETE"
    assert cap["url"].endswith("/a/changes/300")
    assert cap["payload"] is None  # DELETE carries no body


def test_delete_204_success_is_accepted(monkeypatch):
    # The success path must treat 204 (No Content) as OK, not an error.
    cap = _mock(monkeypatch, _NEW, act_status=204)
    r = delete_change(
        "https://gerrit.x.example/c/p/+/300",
        require_owner_email="me@example.com",
        creds=CREDS,
    )
    assert r.skipped is False
    assert cap["method"] == "DELETE"


def test_delete_owner_gate_case_insensitive(monkeypatch):
    cap = _mock(monkeypatch, _NEW, act_status=204)
    r = delete_change(
        "https://gerrit.x.example/c/p/+/300",
        require_owner_email="ME@EXAMPLE.COM",  # different case, same identity
        creds=CREDS,
    )
    assert r.skipped is False
    assert cap["method"] == "DELETE"


def test_delete_not_owner_is_skipped(monkeypatch):
    cap = _mock(monkeypatch, _NEW, act_status=204)
    r = delete_change(
        "https://gerrit.x.example/c/p/+/300",
        require_owner_email="someone-else@example.com",
        creds=CREDS,
    )
    assert r.skipped is True
    assert "not owned by you" in r.skip_reason
    assert "method" not in cap  # no DELETE sent


def test_delete_merged_is_skipped(monkeypatch):
    cap = _mock(
        monkeypatch,
        {
            "_number": 303,
            "status": "MERGED",
            "subject": "x",
            "owner": {"email": "me@example.com"},
        },
        act_status=204,
    )
    r = delete_change(
        "https://gerrit.x.example/c/p/+/303",
        require_owner_email="me@example.com",
        creds=CREDS,
    )
    assert r.skipped is True
    assert "merged" in r.skip_reason
    assert "method" not in cap


def test_delete_dry_run_sends_nothing(monkeypatch):
    cap = _mock(monkeypatch, _NEW, act_status=204)
    r = delete_change(
        "https://gerrit.x.example/c/p/+/300",
        require_owner_email="me@example.com",
        dry_run=True,
        creds=CREDS,
    )
    assert r.dry_run is True
    assert r.skipped is False
    assert "method" not in cap  # dry-run: no DELETE


def test_delete_no_owner_gate_when_guard_none(monkeypatch):
    # require_owner_email=None disables the gate (used only when the local git
    # identity is unknown); the DELETE still goes out.
    cap = _mock(monkeypatch, _NEW, act_status=204)
    r = delete_change(
        "https://gerrit.x.example/c/p/+/300",
        require_owner_email=None,
        creds=CREDS,
    )
    assert r.skipped is False
    assert cap["method"] == "DELETE"


# --------------------------------------------------------------------------
# my_changes — the --all building block
# --------------------------------------------------------------------------


def test_my_changes_builds_urls(monkeypatch):
    from gerrit_vote import my_changes

    listing = [{"_number": 11}, {"_number": 22}, {"no_number": True}]
    body = ")]}'\n" + json.dumps(listing)
    seen = {}

    def fake_request(method, url, auth, payload):
        seen["url"] = url
        return 200, body

    monkeypatch.setattr(core, "_request", fake_request)
    urls = my_changes(CREDS)
    assert urls == [
        "https://gerrit.x.example/c/11",
        "https://gerrit.x.example/c/22",
    ]
    # owner:self status:open (default) is what makes it "my" open changes.
    assert "owner%3Aself" in seen["url"] or "owner:self" in seen["url"]
    assert "open" in seen["url"]


def test_my_changes_status_param(monkeypatch):
    from gerrit_vote import my_changes

    seen = {}

    def fake_request(method, url, auth, payload):
        seen["url"] = url
        return 200, ")]}'\n[]"

    monkeypatch.setattr(core, "_request", fake_request)
    my_changes(CREDS, status="abandoned")
    # restore sweep queries abandoned changes, not open ones.
    assert "abandoned" in seen["url"]


def test_my_changes_error_raises(monkeypatch):
    from gerrit_vote import VoteError, my_changes

    monkeypatch.setattr(core, "_request", lambda *a: (403, ")]}'\n{}"))
    with pytest.raises(VoteError, match="Cannot query your open changes"):
        my_changes(CREDS)


# --------------------------------------------------------------------------
# list_my_changes — owned + review, non-merged, tagged is_owner
# --------------------------------------------------------------------------


def _mock_list(monkeypatch, changes, self_email="me@example.com"):
    """Stub _request: the changes query returns ``changes``; accounts/self
    returns ``self_email``. Records the change-query URL for assertions."""
    seen = {}

    def fake_request(method, url, auth, payload):
        if "/a/accounts/self" in url:
            return 200, ")]}'\n" + json.dumps({"email": self_email})
        seen["url"] = url
        return 200, ")]}'\n" + json.dumps(changes)

    monkeypatch.setattr(core, "_request", fake_request)
    return seen


def test_list_my_changes_tags_owner_vs_review(monkeypatch):
    from gerrit_vote import list_my_changes

    changes = [
        {
            "_number": 10,
            "status": "NEW",
            "subject": "mine open",
            "project": "p",
            "branch": "b",
            "owner": {"email": "me@example.com"},
        },
        {
            "_number": 11,
            "status": "ABANDONED",
            "subject": "mine abandoned",
            "project": "p",
            "branch": "b",
            "owner": {"email": "me@example.com"},
        },
        {
            "_number": 12,
            "status": "NEW",
            "subject": "review for me",
            "project": "p",
            "branch": "b",
            "owner": {"email": "someone@example.com"},
        },
    ]
    seen = _mock_list(monkeypatch, changes)
    items = list_my_changes(CREDS)
    assert [i.number for i in items] == ["10", "11", "12"]
    assert [i.is_owner for i in items] == [True, True, False]
    assert [i.status for i in items] == ["NEW", "ABANDONED", "NEW"]
    # The query unions owner + reviewer and excludes merged, server-side.
    assert "reviewer" in seen["url"]
    assert "merged" in seen["url"]  # -status:merged


def test_list_my_changes_error_raises(monkeypatch):
    from gerrit_vote import VoteError, list_my_changes

    def fake_request(method, url, auth, payload):
        if "/a/accounts/self" in url:
            return 200, ")]}'\n{}"
        return 403, ")]}'\n{}"

    monkeypatch.setattr(core, "_request", fake_request)
    with pytest.raises(VoteError, match="Cannot list your changes"):
        list_my_changes(CREDS)


def test_list_my_changes_all_review_when_self_email_unknown(monkeypatch):
    # If accounts/self can't be read, ownership can't be proven -> everything
    # tags as review (conservative), never crashes.
    from gerrit_vote import list_my_changes

    changes = [
        {"_number": 20, "status": "NEW", "owner": {"email": "me@example.com"}},
    ]

    def fake_request(method, url, auth, payload):
        if "/a/accounts/self" in url:
            return 404, ")]}'\n{}"
        return 200, ")]}'\n" + json.dumps(changes)

    monkeypatch.setattr(core, "_request", fake_request)
    items = list_my_changes(CREDS)
    assert items[0].is_owner is False
