# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Junbo Zheng. All rights reserved.
"""Tests for CLI positional-arg parsing (changes + optional trailing value)."""

import types

import pytest

from gerrit_vote import Credentials, __version__, cli
from gerrit_vote.cli import _is_value, main


@pytest.mark.parametrize("flag", ["--version", "-V"])
def test_version_flag_prints_and_exits(flag, capsys):
    # argparse's version action prints to stdout and raises SystemExit(0).
    with pytest.raises(SystemExit) as exc:
        main([flag])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_is_value_recognizes_vote_range():
    for v in ("-2", "-1", "0", "1", "2"):
        assert _is_value(v) is True


def test_is_value_rejects_change_numbers():
    # Change numbers are large integers, never confused with a vote value.
    assert _is_value("9182632") is False
    assert _is_value("12345") is False


def test_is_value_rejects_urls_and_garbage():
    assert _is_value("https://gerrit.x/c/p/+/1") is False
    assert _is_value("abc") is False
    assert _is_value("3") is False  # out of -2..2 range


def _patch_topic(monkeypatch, urls):
    """Stub credential + topic-query resolution in the CLI namespace."""
    creds = Credentials(url="https://gerrit.x.example", user="u", password="p")
    monkeypatch.setattr(cli, "resolve", lambda host, **kw: creds)
    monkeypatch.setattr(cli, "changes_in_topic", lambda creds, topic: list(urls))


def test_topic_flag_extract_lists_expanded_changes(monkeypatch, capsys):
    _patch_topic(
        monkeypatch,
        ["https://gerrit.x.example/c/100", "https://gerrit.x.example/c/101"],
    )
    rc = main(["--topic", "my-feature", "--extract"])
    out = capsys.readouterr().out.splitlines()
    assert rc == 0
    assert out == [
        "https://gerrit.x.example/c/100",
        "https://gerrit.x.example/c/101",
    ]


def test_topic_url_dry_run_casts_nothing(monkeypatch, capsys):
    _patch_topic(monkeypatch, ["https://gerrit.x.example/c/200"])

    # vote() must not actually be called to send anything on a dry-run; the
    # expansion + listing is enough. Fail loudly if a real vote slips through.
    def _boom(*a, **k):
        raise AssertionError("vote() should run in dry-run, not POST")

    # vote is still invoked (dry_run=True), so stub it to a benign dry result.
    from gerrit_vote import VoteResult

    monkeypatch.setattr(
        cli,
        "vote",
        lambda ch, value, **k: VoteResult(
            change_id="200",
            host="gerrit.x.example",
            subject="s",
            project="p",
            branch="b",
            status="NEW",
            requested=value,
            cast=None,
            downgraded=False,
            dry_run=True,
        ),
    )
    rc = main(["https://gerrit.x.example/q/topic:my-feature", "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "200" in out
    assert "dry-run" in out


def test_topic_no_open_changes_message(monkeypatch, capsys):
    _patch_topic(monkeypatch, [])
    rc = main(["--topic", "empty", "--extract"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "no open changes found" in err


def test_list_labels_prints_permitted(monkeypatch, capsys):
    # --list-labels queries the server and prints label -> allowed values,
    # then exits without voting.
    monkeypatch.setattr(
        cli,
        "permitted_labels",
        lambda change, **kw: {
            "Code-Review": ["-2", "-1", "0", "+1", "+2"],
            "Verified": ["-1", "0", "+1"],
        },
    )
    rc = main(["--list-labels", "https://gerrit.x.example/c/p/+/300"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Code-Review" in out
    assert "Verified" in out
    assert "+2" in out


def test_list_labels_empty_reports_none(monkeypatch, capsys):
    monkeypatch.setattr(cli, "permitted_labels", lambda change, **kw: {})
    rc = main(["--list-labels", "https://gerrit.x.example/c/p/+/301"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "No labels permitted" in out


def test_menu_values_descending_from_permitted():
    # Server values (unordered, padded) become a clean descending int list.
    assert cli._menu_values(
        {"Code-Review": ["+1", " 0", "-1", "+2", "-2"]}, "Code-Review"
    ) == [2, 1, 0, -1, -2]
    # A 0/+1 label offers only those two.
    assert cli._menu_values({"Presubmit-Ready": [" 0", "+1"]}, "Presubmit-Ready") == [
        1,
        0,
    ]


def test_menu_values_falls_back_when_absent_or_none():
    # None (meta fetch failed) or label absent -> full -2..+2 fallback.
    assert cli._menu_values(None, "Code-Review") == [2, 1, 0, -1, -2]
    assert cli._menu_values({"Verified": ["+1"]}, "Code-Review") == [2, 1, 0, -1, -2]


def test_menu_fmt_pads_zero_for_alignment():
    assert cli._menu_fmt(2) == "+2"
    assert cli._menu_fmt(0) == " 0"  # padded to align with +2 / -1
    assert cli._menu_fmt(-1) == "-1"


def test_is_owned_by_me_matches_git_identity(monkeypatch):
    monkeypatch.setattr(cli, "git_identity_email", lambda: "me@example.com")
    assert cli._is_owned_by_me("ME@Example.com") is True  # case-insensitive
    assert cli._is_owned_by_me("other@example.com") is False
    assert cli._is_owned_by_me("") is False


def test_is_owned_by_me_false_when_no_git_identity(monkeypatch):
    monkeypatch.setattr(cli, "git_identity_email", lambda: None)
    assert cli._is_owned_by_me("me@example.com") is False


# ---------------------------------------------------------------------------
# delete flow: preview (dry-run) -> interactive y/N -> real delete
# ---------------------------------------------------------------------------


def _spy_delete_many(monkeypatch):
    """Replace cli._delete_many with a spy recording each dry_run value.

    Returns the list of dry_run booleans it was called with, so a test can
    assert the preview (True) and/or the real delete (False) happened — without
    ever touching the network.
    """
    calls: list[bool] = []

    def fake(changes, dry_run, user=None, password=None):
        calls.append(dry_run)
        return 0

    monkeypatch.setattr(cli, "_delete_many", fake)
    return calls


def test_delete_confirm_yes_deletes(monkeypatch):
    calls = _spy_delete_many(monkeypatch)
    # The arrow-key confirm (which lists the changes itself) returns True.
    monkeypatch.setattr(cli, "_confirm_action", lambda *a, **k: True)
    rc = main(["--delete", "https://gerrit.x.example/c/p/+/300"])
    assert rc == 0
    # No separate preview: exactly one real delete (dry_run=False).
    assert calls == [False]


def test_delete_confirm_no_aborts(monkeypatch, capsys):
    calls = _spy_delete_many(monkeypatch)
    # The arrow-key confirm returns False (user picked "No" / Esc).
    monkeypatch.setattr(cli, "_confirm_action", lambda *a, **k: False)
    rc = main(["--delete", "https://gerrit.x.example/c/p/+/300"])
    assert rc == 1
    assert calls == []  # nothing deleted, no preview
    assert "Aborted" in capsys.readouterr().out


def test_delete_dry_run_shows_preview_only(monkeypatch):
    calls = _spy_delete_many(monkeypatch)
    # confirm must never be reached on --dry-run; make it explode if it is.
    monkeypatch.setattr(
        cli, "_confirm_action", lambda *a, **k: pytest.fail("prompted!")
    )
    rc = main(["--delete", "--dry-run", "https://gerrit.x.example/c/p/+/300"])
    assert rc == 0
    assert calls == [True]  # preview only (dry_run=True)


def test_delete_force_skips_prompt(monkeypatch):
    calls = _spy_delete_many(monkeypatch)
    monkeypatch.setattr(
        cli, "_confirm_action", lambda *a, **k: pytest.fail("prompted!")
    )
    rc = main(["--delete", "--force-delete", "https://gerrit.x.example/c/p/+/300"])
    assert rc == 0
    assert calls == [False]  # straight to delete, no preview, no prompt


def test_delete_non_tty_without_force_stops(monkeypatch, capsys):
    calls = _spy_delete_many(monkeypatch)
    # _confirm_action returns False on a non-TTY (its own guard), so the flow
    # aborts without deleting. Simulate that here.
    monkeypatch.setattr(cli, "_confirm_action", lambda *a, **k: False)
    rc = main(["--delete", "https://gerrit.x.example/c/p/+/300"])
    assert rc == 1
    assert calls == []  # nothing deleted; never blind-deletes


# ---------------------------------------------------------------------------
# --all: sweep owner:self open changes across all hosts
# ---------------------------------------------------------------------------


def _patch_all(monkeypatch, per_host):
    """Stub all_hosts + my_changes in the CLI namespace.

    per_host: list of URL-lists, one per fake host.
    """
    creds = [
        Credentials(url=f"https://h{i}.example", user="u", password="p")
        for i in range(len(per_host))
    ]
    monkeypatch.setattr(cli, "all_hosts", lambda **kw: creds)
    mapping = {c.url: urls for c, urls in zip(creds, per_host)}
    monkeypatch.setattr(cli, "my_changes", lambda c, status="open": mapping[c.url])


def test_all_requires_action(capsys):
    rc = main(["--all"])
    assert rc == 2
    assert "requires --abandon, --restore or --delete" in capsys.readouterr().err


def test_all_delete_sweeps_all_hosts(monkeypatch):
    _patch_all(
        monkeypatch,
        [
            ["https://h0.example/c/1"],
            ["https://h1.example/c/2", "https://h1.example/c/3"],
        ],
    )
    seen = {}

    def fake_flow(urls, *, dry_run, force, user=None, password=None):
        seen["urls"] = urls
        return 0

    monkeypatch.setattr(cli, "_delete_flow", fake_flow)
    rc = main(["--all", "--delete"])
    assert rc == 0
    # All three changes across the two hosts are targeted.
    assert seen["urls"] == [
        "https://h0.example/c/1",
        "https://h1.example/c/2",
        "https://h1.example/c/3",
    ]


def test_all_abandon_confirms_then_calls(monkeypatch):
    from gerrit_vote import abandon

    _patch_all(monkeypatch, [["https://h0.example/c/1"]])
    monkeypatch.setattr(cli, "_confirm_action", lambda *a, **k: True)
    seen = {}

    def fake_lifecycle(urls, action, message, dry_run, user=None, password=None):
        seen["urls"] = urls
        seen["action"] = action
        return 0

    monkeypatch.setattr(cli, "_lifecycle_many", fake_lifecycle)
    rc = main(["--all", "--abandon"])
    assert rc == 0
    assert seen["urls"] == ["https://h0.example/c/1"]
    assert seen["action"] is abandon


def test_all_restore_sweeps_abandoned(monkeypatch):
    from gerrit_vote import restore

    # --restore --all must query the ABANDONED status, and route to restore().
    seen = {}

    def fake_my_changes(creds, status="open"):
        seen["status"] = status
        return ["https://h0.example/c/9"]

    creds = [Credentials(url="https://h0.example", user="u", password="p")]
    monkeypatch.setattr(cli, "all_hosts", lambda **kw: creds)
    monkeypatch.setattr(cli, "my_changes", fake_my_changes)
    monkeypatch.setattr(cli, "_confirm_action", lambda *a, **k: True)

    def fake_lifecycle(urls, action, message, dry_run, user=None, password=None):
        seen["urls"] = urls
        seen["action"] = action
        return 0

    monkeypatch.setattr(cli, "_lifecycle_many", fake_lifecycle)
    rc = main(["--all", "--restore"])
    assert rc == 0
    assert seen["status"] == "abandoned"  # not "open"
    assert seen["urls"] == ["https://h0.example/c/9"]
    assert seen["action"] is restore


def test_all_empty_reports_nothing(monkeypatch, capsys):
    _patch_all(monkeypatch, [[], []])
    rc = main(["--all", "--delete"])
    assert rc == 0
    assert "no open changes found for you" in capsys.readouterr().out


# --------------------------------------------------------------------------
# --all --delete scope menu (open / abandoned / both / quit)
# --------------------------------------------------------------------------


def _tty_stdin(monkeypatch):
    """Force cli's sys.stdin.isatty() True so the scope menu branch runs."""
    monkeypatch.setattr(cli.sys, "stdin", types.SimpleNamespace(isatty=lambda: True))


def _stub_all_hosts(monkeypatch):
    creds = [Credentials(url="https://h0.example", user="u", password="p")]
    monkeypatch.setattr(cli, "all_hosts", lambda **kw: creds)
    return creds


def test_all_delete_abandoned_sweeps_abandoned(monkeypatch):
    # Picking "abandoned" in the scope menu sweeps ABANDONED, not open.
    seen = {}

    def fake_my_changes(creds, status="open"):
        seen.setdefault("statuses", []).append(status)
        return [f"https://h0.example/c/{status}"]

    _stub_all_hosts(monkeypatch)
    monkeypatch.setattr(cli, "my_changes", fake_my_changes)
    _tty_stdin(monkeypatch)
    monkeypatch.setattr(cli, "_pick_delete_scope", lambda: "abandoned")

    captured = {}

    def fake_delete_flow(urls, **kw):
        captured["urls"] = urls
        return 0

    monkeypatch.setattr(cli, "_delete_flow", fake_delete_flow)

    rc = main(["--all", "--delete"])
    assert rc == 0
    assert seen["statuses"] == ["abandoned"]
    assert captured["urls"] == ["https://h0.example/c/abandoned"]


def test_all_delete_both_sweeps_both_and_dedups(monkeypatch):
    # Picking "all" queries open AND abandoned, then dedups overlapping URLs.
    seen = {}

    def fake_my_changes(creds, status="open"):
        seen.setdefault("statuses", []).append(status)
        # Same URL returned for both statuses -> exercises dedup.
        return ["https://h0.example/c/1"]

    _stub_all_hosts(monkeypatch)
    monkeypatch.setattr(cli, "my_changes", fake_my_changes)
    _tty_stdin(monkeypatch)
    monkeypatch.setattr(cli, "_pick_delete_scope", lambda: "all")

    captured = {}

    def fake_delete_flow(urls, **kw):
        captured["urls"] = urls
        return 0

    monkeypatch.setattr(cli, "_delete_flow", fake_delete_flow)

    rc = main(["--all", "--delete"])
    assert rc == 0
    assert seen["statuses"] == ["open", "abandoned"]
    assert captured["urls"] == ["https://h0.example/c/1"]  # deduped to one


def test_all_delete_dry_run_still_shows_scope_menu(monkeypatch):
    # Regression: --dry-run used to force "open" (menu skipped). The scope
    # picker is not a destructive gate, so dry-run must still show it.
    called = {"pick": False}

    def fake_my_changes(creds, status="open"):
        return [f"https://h0.example/c/{status}"]

    _stub_all_hosts(monkeypatch)
    monkeypatch.setattr(cli, "my_changes", fake_my_changes)
    _tty_stdin(monkeypatch)

    def fake_pick():
        called["pick"] = True
        return "abandoned"

    monkeypatch.setattr(cli, "_pick_delete_scope", fake_pick)

    captured = {}

    def fake_delete_flow(urls, dry_run=False, **kw):
        captured["urls"] = urls
        captured["dry_run"] = dry_run
        return 0

    monkeypatch.setattr(cli, "_delete_flow", fake_delete_flow)

    rc = main(["--all", "--delete", "--dry-run"])
    assert rc == 0
    assert called["pick"] is True  # menu shown under dry-run
    assert captured["dry_run"] is True
    assert captured["urls"] == ["https://h0.example/c/abandoned"]


def test_all_delete_quit_aborts_before_sweep(monkeypatch):
    # Canceling the scope menu aborts before any sweep query is sent.
    _stub_all_hosts(monkeypatch)
    swept = []

    def fake_my_changes(creds, status="open"):
        swept.append(status)
        return []

    monkeypatch.setattr(cli, "my_changes", fake_my_changes)
    _tty_stdin(monkeypatch)
    monkeypatch.setattr(cli, "_pick_delete_scope", lambda: cli._QUIT)

    rc = main(["--all", "--delete"])
    assert rc == 1
    assert swept == []  # never queried


def test_abandon_restore_delete_mutually_exclusive(capsys):
    rc = main(["--abandon", "--restore", "https://gerrit.x/c/1"])
    assert rc == 2
    assert "cannot be combined" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# _confirm_action default-focus order (reversible -> Yes first, delete -> No)
# ---------------------------------------------------------------------------


def _capture_confirm_choices(monkeypatch, verb):
    """Return the choice list _confirm_action builds for ``verb`` (values in
    order), without a real terminal."""
    monkeypatch.setattr(cli, "change_meta", lambda u, **k: None)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)

    import questionary

    captured = {}

    class _FakeQ:
        def __init__(self):
            class _App:
                key_bindings = None

            self.application = _App()

        def ask(self):
            return False

    def fake_select(msg, choices, **kw):
        captured["values"] = [c.value for c in choices]
        return _FakeQ()

    monkeypatch.setattr(questionary, "select", fake_select)
    cli._confirm_action(["https://gerrit.x/c/1"], verb)
    return captured["values"]


def test_confirm_focus_reversible_yes_first(monkeypatch):
    # abandon / restore are reversible -> "Yes" (True) is the first row, so a
    # reflexive Enter proceeds.
    assert _capture_confirm_choices(monkeypatch, "abandon")[0] is True
    assert _capture_confirm_choices(monkeypatch, "restore")[0] is True


def test_confirm_focus_delete_no_first(monkeypatch):
    # delete is irreversible -> "No" (False) is the first row, so a reflexive
    # Enter cancels.
    assert _capture_confirm_choices(monkeypatch, "delete")[0] is False


# ---------------------------------------------------------------------------
# summary tally names the action even when every change failed
# ---------------------------------------------------------------------------


def test_summary_all_failed_names_action(capsys):
    # results empty (all failed) — the tally must say the real action, not the
    # default "voted". Regression for the "0 voted, 1 failed" on --restore bug.
    cli._print_summary([], [("https://gerrit.x/c/1", "boom")], action="restore")
    out = capsys.readouterr().out
    assert "0 restored" in out
    assert "voted" not in out


# ---------------------------------------------------------------------------
# --list: read-only listing of owned + review changes across hosts
# ---------------------------------------------------------------------------


def test_list_prints_owned_and_review(monkeypatch, capsys):
    from gerrit_vote import ChangeListItem

    creds = [Credentials(url="https://h0.example", user="u", password="p")]
    monkeypatch.setattr(cli, "all_hosts", lambda **kw: creds)
    items = [
        ChangeListItem(
            number="10",
            url="https://h0.example/c/10",
            subject="mine",
            status="NEW",
            project="group/repo/app",
            branch="my-branch",
            owner_email="me@example.com",
            is_owner=True,
        ),
        ChangeListItem(
            number="12",
            url="https://h0.example/c/12",
            subject="review me",
            status="NEW",
            project="group/repo/app",
            branch="my-branch",
            owner_email="other@example.com",
            is_owner=False,
        ),
    ]
    monkeypatch.setattr(cli, "list_my_changes", lambda c: items)
    rc = main(["--list"])
    out = capsys.readouterr().out
    assert rc == 0
    # Table columns + relationship tags.
    assert "ID" in out and "STATUS" in out and "URL" in out
    assert "REPO" in out and "BRANCH" in out and "SUBJECT" in out
    assert "group/repo/app" in out  # repo column value
    assert "my-branch" in out  # branch column value
    assert "mine" in out and "review me" in out  # the subjects show
    assert "owner" in out  # the owned row
    assert "REVIEW" in out  # the review row, highlighted
    assert "https://h0.example/c/10" in out
    assert "https://h0.example/c/12" in out
    assert "2 change(s) across 1 host(s)" in out
    assert "1 need your review" in out


def test_list_per_host_error_is_inline(monkeypatch, capsys):
    from gerrit_vote import VoteError

    creds = [Credentials(url="https://h0.example", user="u", password="p")]
    monkeypatch.setattr(cli, "all_hosts", lambda **kw: creds)

    def boom(c):
        raise VoteError("nope")

    monkeypatch.setattr(cli, "list_my_changes", boom)
    rc = main(["--list"])
    out = capsys.readouterr().out
    assert rc == 1  # a per-host failure makes the overall rc non-zero
    assert "error: nope" in out  # reported inline, not crashed


# ---------------------------------------------------------------------------
# _confirm_action: arrow-key select menu (questionary stubbed)
# ---------------------------------------------------------------------------


def test_confirm_action_non_tty_returns_false(monkeypatch, capsys):
    # A destructive confirm never proceeds unattended.
    monkeypatch.setattr(cli, "change_meta", lambda u, **k: None)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    assert cli._confirm_action(["https://gerrit.x/c/1"], "delete") is False
    assert "not a TTY" in capsys.readouterr().out


def test_confirm_action_yes_true_no_false(monkeypatch):
    # Stub questionary.select so .ask() returns the chosen value, exercising the
    # True/False mapping without a real terminal.
    monkeypatch.setattr(cli, "change_meta", lambda u, **k: None)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)

    import questionary

    class _FakeQ:
        def __init__(self, result):
            self._result = result

            class _App:
                key_bindings = None

            self.application = _App()

        def ask(self):
            return self._result

    monkeypatch.setattr(questionary, "select", lambda *a, **k: _FakeQ(True))
    assert cli._confirm_action(["https://gerrit.x/c/1"], "delete") is True

    monkeypatch.setattr(questionary, "select", lambda *a, **k: _FakeQ(False))
    assert cli._confirm_action(["https://gerrit.x/c/1"], "abandon") is False

    # Esc / Ctrl-C -> ask() returns None -> treated as cancel (False).
    monkeypatch.setattr(questionary, "select", lambda *a, **k: _FakeQ(None))
    assert cli._confirm_action(["https://gerrit.x/c/1"], "delete") is False


# ---------------------------------------------------------------------------
# --related-topics + clipboard auto-expand
# ---------------------------------------------------------------------------


def _patch_expansion(monkeypatch, *, related=None, topic_of=None, topic_urls=None):
    """Stub credential resolution + the three expansion primitives in cli.

    - related:    {url: [chain urls]}   (default: origin-only, like the real endpoint)
    - topic_of:   {url: topic}          (default: no topic -> None)
    - topic_urls: {topic: [open urls]}  (default: empty)

    Returns a dict recording which primitives were called, so a test can assert
    that expansion did (or did not) fire.
    """
    creds = Credentials(url="https://gerrit.x.example", user="u", password="p")
    monkeypatch.setattr(cli, "resolve", lambda host, **kw: creds)
    related = related or {}
    topic_of = topic_of or {}
    topic_urls = topic_urls or {}
    calls = {"related": [], "change_topic": [], "changes_in_topic": []}

    def _related(c, u):
        calls["related"].append(u)
        return list(related.get(u, [u]))  # origin is always present

    def _change_topic(c, u):
        calls["change_topic"].append(u)
        return topic_of.get(u)

    def _changes_in_topic(c, t):
        calls["changes_in_topic"].append(t)
        return list(topic_urls.get(t, []))

    monkeypatch.setattr(cli, "related_changes", _related)
    monkeypatch.setattr(cli, "change_topic", _change_topic)
    monkeypatch.setattr(cli, "changes_in_topic", _changes_in_topic)
    return calls


def _stub_vote_spy(monkeypatch):
    """Replace cli.vote with a spy recording each change URL it votes on."""
    voted: list[str] = []

    def _vote(ch, value, **k):
        voted.append(ch)
        from gerrit_vote import VoteResult

        return VoteResult(
            change_id=ch.rsplit("/", 1)[-1],
            host="gerrit.x.example",
            subject="s",
            project="p",
            branch="b",
            status="NEW",
            requested=value,
            cast=None,
            downgraded=False,
            dry_run=k.get("dry_run", False),
        )

    monkeypatch.setattr(cli, "vote", _vote)
    return voted


def test_related_topics_expands_topic_siblings(monkeypatch):
    # --related-topics reads the seed's topic and pulls in its open siblings.
    seed = "https://gerrit.x.example/c/p/+/100"
    sib = "https://gerrit.x.example/c/200"
    _patch_expansion(
        monkeypatch,
        topic_of={seed: "my-feature"},
        topic_urls={"my-feature": [sib]},
    )
    voted = _stub_vote_spy(monkeypatch)
    rc = main(["--related-topics", "--dry-run", seed])
    assert rc == 0
    assert seed in voted
    assert sib in voted  # sibling pulled in via the seed's topic


def test_related_topics_groups_by_host(monkeypatch):
    # Two hosts, each carrying a change with the SAME topic name "feat". A topic
    # query is host-local, so host A's "feat" must be queried with host A's
    # creds (returning c/2) and host B's "feat" with host B's creds (returning
    # c/20) — never cross-host. Locks the by_host grouping: a bug that queried
    # every topic on one host's creds would return the wrong siblings.
    creds_a = Credentials(url="https://gerrit.a.example", user="u", password="p")
    creds_b = Credentials(url="https://gerrit.b.example", user="u", password="p")

    def _resolve(host, **kw):
        return creds_a if host == "gerrit.a.example" else creds_b

    topics = {
        "https://gerrit.a.example/c/1": "feat",
        "https://gerrit.b.example/c/10": "feat",
    }
    # Siblings keyed by creds.url so a cross-host query returns the wrong set
    # and the assertion fails.
    siblings = {
        creds_a.url: {"feat": ["https://gerrit.a.example/c/2"]},
        creds_b.url: {"feat": ["https://gerrit.b.example/c/20"]},
    }
    queried: list[tuple[str, str]] = []  # (creds.url, topic) pairs

    def _changes_in_topic(c, t):
        queried.append((c.url, t))
        return siblings.get(c.url, {}).get(t, [])

    monkeypatch.setattr(cli, "resolve", _resolve)
    monkeypatch.setattr(cli, "change_topic", lambda c, u: topics.get(u))
    monkeypatch.setattr(cli, "changes_in_topic", _changes_in_topic)
    voted = _stub_vote_spy(monkeypatch)

    rc = main(
        [
            "--related-topics",
            "--dry-run",
            "https://gerrit.a.example/c/1",
            "https://gerrit.b.example/c/10",
        ]
    )
    assert rc == 0
    assert set(voted) == {
        "https://gerrit.a.example/c/1",
        "https://gerrit.a.example/c/2",
        "https://gerrit.b.example/c/10",
        "https://gerrit.b.example/c/20",
    }
    # Each host's "feat" was queried on that host's own creds.
    assert (creds_a.url, "feat") in queried
    assert (creds_b.url, "feat") in queried


def test_clipboard_auto_expands_related_and_topics(monkeypatch):
    # A bare `gerrit-vote` (clipboard source) auto-enables --related AND
    # --related-topics: chain + topic siblings are all voted on.
    seed = "https://gerrit.x.example/c/100"
    parent = "https://gerrit.x.example/c/101"  # chain ancestor
    sib = "https://gerrit.x.example/c/200"  # topic sibling
    monkeypatch.setattr(cli, "_read_clipboard", lambda: seed)
    calls = _patch_expansion(
        monkeypatch,
        related={seed: [seed, parent]},
        topic_of={seed: "feat", parent: "feat"},
        topic_urls={"feat": [sib]},
    )
    voted = _stub_vote_spy(monkeypatch)
    rc = main(["--dry-run"])  # bare invocation -> clipboard source
    assert rc == 0
    assert set(voted) == {seed, parent, sib}
    assert calls["related"]  # --related auto-fired
    assert calls["change_topic"]  # --related-topics auto-fired


def test_clipboard_delete_does_not_auto_expand(monkeypatch):
    # Destructive actions never auto-expand on clipboard: --delete acts on the
    # pasted URL only, and related_changes/change_topic are never called.
    seed = "https://gerrit.x.example/c/100"
    monkeypatch.setattr(cli, "_read_clipboard", lambda: seed)
    calls = _patch_expansion(monkeypatch)  # no expansions configured
    captured = {}

    def _flow(urls, *, dry_run, force, user=None, password=None):
        captured["urls"] = list(urls)
        return 0

    monkeypatch.setattr(cli, "_delete_flow", _flow)
    rc = main(["--delete", "--force-delete"])
    assert rc == 0
    assert captured["urls"] == [seed]
    assert calls["related"] == []
    assert calls["change_topic"] == []


def test_explicit_url_does_not_auto_expand(monkeypatch):
    # Explicit positional URL = precise path: no auto-expand, vote on it alone.
    seed = "https://gerrit.x.example/c/p/+/100"
    calls = _patch_expansion(monkeypatch)
    voted = _stub_vote_spy(monkeypatch)
    rc = main(["--dry-run", seed])
    assert rc == 0
    assert voted == [seed]
    assert calls["related"] == []
    assert calls["change_topic"] == []


def test_clipboard_extract_does_not_auto_expand(monkeypatch, capsys):
    # --extract is a pure "parse my clipboard" preview: it prints the pasted
    # links only, no chain/topic expansion (--dry-run is the expanded preview).
    seed = "https://gerrit.x.example/c/100"
    monkeypatch.setattr(cli, "_read_clipboard", lambda: seed)
    calls = _patch_expansion(monkeypatch)  # would expand if asked
    rc = main(["--extract"])
    out = capsys.readouterr().out.splitlines()
    assert rc == 0
    assert out == [seed]  # only the pasted link, nothing expanded
    assert calls["related"] == []
    assert calls["change_topic"] == []


# ---------------------------------------------------------------------------
# menu value rows: union of permitted values across the batch
# ---------------------------------------------------------------------------


def _capture_menu_values(monkeypatch, metas):
    """Run _confirm_and_pick_value with stubbed change_meta + questionary,
    returning the int vote values the menu offered (top first)."""
    monkeypatch.setattr(cli, "change_meta", lambda u, **k: metas.pop(0))
    monkeypatch.setattr(cli, "git_identity_email", lambda: None)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)

    import questionary

    captured = {}

    class _FakeQ:
        def __init__(self):
            class _App:
                key_bindings = None

            self.application = _App()

        def ask(self):
            return cli._QUIT  # cancel — we only care about the offered rows

    def fake_select(msg, choices, **kw):
        captured["values"] = [c.value for c in choices]
        return _FakeQ()

    monkeypatch.setattr(questionary, "select", fake_select)
    cli._confirm_and_pick_value(
        [f"https://gerrit.x.example/c/{i}" for i in range(len(metas))],
        "Code-Review",
    )
    return [v for v in captured["values"] if isinstance(v, int)]


def test_menu_offers_union_of_batch_permissions(monkeypatch):
    # First change is +1-only, second is +2-capable. The menu must still offer
    # +2 (the highest value permitted on ANY change), not cap at the first
    # change's +1 — otherwise the whole batch ends up +1.
    from gerrit_vote import ChangeMeta

    metas = [
        ChangeMeta(
            permitted={"Code-Review": ["-1", " 0", "+1"]},
            owner_email="",
            author_email="",
            subject="a",
            status="NEW",
        ),
        ChangeMeta(
            permitted={"Code-Review": ["-2", "-1", " 0", "+1", "+2"]},
            owner_email="",
            author_email="",
            subject="b",
            status="NEW",
        ),
    ]
    values = _capture_menu_values(monkeypatch, metas)
    assert values[0] == 2  # top of the menu is +2 despite the +1-only first change
    assert 2 in values


def test_menu_caps_at_batch_max_when_all_plus1(monkeypatch):
    # When NO change in the batch permits +2, the menu correctly caps at +1.
    from gerrit_vote import ChangeMeta

    metas = [
        ChangeMeta(
            permitted={"Code-Review": ["-1", " 0", "+1"]},
            owner_email="",
            author_email="",
            subject="a",
            status="NEW",
        ),
    ]
    values = _capture_menu_values(monkeypatch, metas)
    assert values == [1, 0, -1]
    assert 2 not in values
