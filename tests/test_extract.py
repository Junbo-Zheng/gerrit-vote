# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Junbo Zheng. All rights reserved.
"""Tests for change-link extraction and parsing."""

from gerrit_vote import dedup_changes, extract_changes, parse_change


def test_extract_basic_and_dedup():
    text = """
    look at https://gerrit.pt.example/c/proj/a/+/9182715 and
    https://gerrit.pt.example/c/proj/b/+/9182632/3 (patchset dropped)
    dup https://gerrit.pt.example/c/proj/a/+/9182715
    """
    urls = extract_changes(text)
    assert urls == [
        "https://gerrit.pt.example/c/proj/a/+/9182715",
        "https://gerrit.pt.example/c/proj/b/+/9182632",
    ]


def test_extract_ignores_non_gerrit():
    text = (
        "doc https://docs.example.cn/x/abc and "
        "pr https://github.com/foo/bar/pull/12 "
        "real https://gerrit.odm.example/c/p/+/55001"
    )
    assert extract_changes(text) == [
        "https://gerrit.odm.example/c/p/+/55001",
    ]


def test_extract_trailing_cjk_punctuation():
    text = (
        "顺便 https://gerrit.odm.example/c/foo/bar/+/55001。"
        "还有 https://gerrit.pt.example/c/x/y/+/8888，下一句"
    )
    assert extract_changes(text) == [
        "https://gerrit.odm.example/c/foo/bar/+/55001",
        "https://gerrit.pt.example/c/x/y/+/8888",
    ]


def test_extract_old_fragment_style():
    text = "old https://gerrit.pt.example/#/c/9182723/"
    assert extract_changes(text) == ["https://gerrit.pt.example/c/9182723"]


def test_extract_mixed_hosts_preserved():
    text = (
        "https://gerrit.pt.example/c/p/+/1 "
        "https://gerrit.odm.example/c/p/+/2 "
        "https://gerrit.thirdparty.example/c/p/+/3"
    )
    urls = extract_changes(text)
    hosts = [u.split("/")[2] for u in urls]
    assert hosts == [
        "gerrit.pt.example",
        "gerrit.odm.example",
        "gerrit.thirdparty.example",
    ]


def test_extract_comma_delimited_csv_row():
    # CSV/table paste: the change URL is immediately followed by ",col,col,...".
    # The comma must terminate the URL, else the trailing columns get swallowed
    # and the change id no longer parses (dropping the change silently).
    text = (
        "task,TASK-1,"
        "https://gerrit.odm.example/c/group/repo/app/+/55001,"
        "board,branch,col,name\n"
    )
    assert extract_changes(text) == [
        "https://gerrit.odm.example/c/group/repo/app/+/55001",
    ]


def test_extract_tab_delimited_excel_with_multiline_cell():
    # Excel/spreadsheet paste: columns are TAB-separated (not commas), and a
    # cell may be a quoted MULTILINE value (embedded newlines). Extraction scans
    # the whole text for URL tokens (TAB is whitespace, so it terminates a URL),
    # so both the row layout and the multiline cell must not disturb the result.
    text = (
        "title\thttps://gerrit.odm.example/c/g/r/app/+/55001\tdone\tapp\n"
        "sleep\thttps://gerrit.odm.example/c/g/r/app/+/55002\tdone\tapp\t"
        'TASK-1\t"1) foo changed A to B\n'
        "2) bar does not return\n"
        '3) queue size set to 5"\tcol\tPass\n'
        "car\thttps://gerrit.odm.example/c/v/boards/mm/+/55003\tdone\n"
    )
    assert extract_changes(text) == [
        "https://gerrit.odm.example/c/g/r/app/+/55001",
        "https://gerrit.odm.example/c/g/r/app/+/55002",
        "https://gerrit.odm.example/c/v/boards/mm/+/55003",
    ]


def test_parse_change_url_with_plus():
    host, cid = parse_change("https://gerrit.pt.example/c/proj/+/9182715")
    assert host == "gerrit.pt.example"
    assert cid == "9182715"


def test_parse_change_bare_number():
    host, cid = parse_change("9182715")
    assert host is None
    assert cid == "9182715"


# ---------------------------------------------------------------------------
# Bare URLs pasted without an "https://" scheme (chat / clipboard common case)
# ---------------------------------------------------------------------------


def test_extract_bare_url_no_scheme():
    # A Gerrit change URL pasted straight from chat with no "https://" prefix.
    text = "gerrit.pt.example/c/proj/app/+/9182715"
    assert extract_changes(text) == [
        "https://gerrit.pt.example/c/proj/app/+/9182715",
    ]


def test_extract_bare_url_in_chat_line():
    # A chat message where the URL drops the scheme and sits mid-sentence on
    # its own line, with a trailing "(gl)" note — the bare-host branch must
    # still pick up both changes.
    text = (
        "please review these two changes:\n"
        "gerrit.pt.example/c/group/repo/app/+/9182715  (gl)\n"
        "gerrit.pt.example/c/group/repo/app/+/9182632 thanks"
    )
    assert extract_changes(text) == [
        "https://gerrit.pt.example/c/group/repo/app/+/9182715",
        "https://gerrit.pt.example/c/group/repo/app/+/9182632",
    ]


def test_extract_bare_url_mixed_with_schemed():
    text = "https://gerrit.pt.example/c/p/+/1 gerrit.odm.example/c/p/+/2"
    assert extract_changes(text) == [
        "https://gerrit.pt.example/c/p/+/1",
        "https://gerrit.odm.example/c/p/+/2",
    ]


def test_extract_bare_url_dedup_with_schemed():
    # Bare and schemed forms of the same change collapse to one entry.
    text = (
        "https://gerrit.pt.example/c/group/repo/app/+/9182715 "
        "gerrit.pt.example/c/group/repo/app/+/9182715"
    )
    assert extract_changes(text) == [
        "https://gerrit.pt.example/c/group/repo/app/+/9182715",
    ]


def test_extract_bare_hostname_without_change_path_rejected():
    # A bare hostname mentioned in prose (no change path) must not match.
    assert extract_changes("deployed at gerrit.pt.example today") == []


# A Gerrit Change-Id: "I" + 40 hex. Placeholder, NOT copied from a real change.
_CID = "I1234567890abcdef1234567890abcdef12345678"
# 8-char short form (Gerrit tab-title / short-link form). Placeholder only.
_CID_SHORT = "I12345678"


def test_extract_change_id_query_url():
    # /q/I<Change-Id> carries no change number — kept as-is (canonical) so it
    # is not silently dropped. resolve_change_ids() turns it into /c/<number>.
    text = f"https://gerrit.odm.example/q/{_CID}"
    assert extract_changes(text) == [f"https://gerrit.odm.example/q/{_CID}"]


def test_extract_change_id_mixed_with_change_numbers():
    text = f"https://gerrit.odm.example/c/p/+/55001 https://gerrit.odm.example/q/{_CID}"
    assert extract_changes(text) == [
        "https://gerrit.odm.example/c/p/+/55001",
        f"https://gerrit.odm.example/q/{_CID}",
    ]


def test_extract_rejects_non_change_id_q_path():
    # A /q/ path that is NOT a Change-Id (e.g. a free-text/topic query) is not
    # a change link — must not be mistaken for one by the new /q/ branch.
    assert extract_changes("https://gerrit.odm.example/q/topic:foo") == []
    assert extract_changes("https://gerrit.odm.example/q/some+search") == []


def test_extract_dedup_same_change_two_url_forms():
    # Full-path form (as pasted from a Gerrit UI) and short form (as returned
    # by the Gerrit API / related_changes) point at the same change — both must
    # collapse to one entry, not vote twice.
    text = (
        "https://gerrit.pt.example/c/group/repo/app/+/9182715 "
        "https://gerrit.pt.example/c/9182715"
    )
    assert extract_changes(text) == [
        "https://gerrit.pt.example/c/group/repo/app/+/9182715",
    ]


def test_dedup_changes_keeps_first_form():
    urls = [
        "https://gerrit.pt.example/c/9182715",  # short form first
        "https://gerrit.pt.example/c/group/repo/app/+/9182715",
        "https://gerrit.pt.example/c/proj/b/+/9182632",
    ]
    assert dedup_changes(urls) == [
        "https://gerrit.pt.example/c/9182715",
        "https://gerrit.pt.example/c/proj/b/+/9182632",
    ]


def test_dedup_changes_separates_hosts():
    # Same change number on different hosts are distinct changes.
    urls = [
        "https://gerrit.pt.example/c/123",
        "https://gerrit.odm.example/c/123",
    ]
    assert dedup_changes(urls) == urls


# ---------------------------------------------------------------------------
# Bare Change-Id token extraction (no URL wrapper)
# ---------------------------------------------------------------------------


def test_extract_bare_change_id_short_form():
    # Gerrit tab title: "some change title (I12345678) · Gerrit Code Review".
    text = f"some change title ({_CID_SHORT}) · Gerrit Code Review"
    assert extract_changes(text) == [f"change-id:{_CID_SHORT}"]


def test_extract_bare_change_id_full_form():
    # Commit-message line: "Change-Id: I<40hex>".
    text = f"Change-Id: {_CID}"
    assert extract_changes(text) == [f"change-id:{_CID}"]


def test_extract_bare_change_id_in_tab_title_scenario():
    # Clipboard shape that prompted this feature: two Gerrit tab titles (each
    # carrying a bare Change-Id in parens) + a chat line, no URLs anywhere.
    # Both tokens are extracted as carriers.
    text = (
        f"some change title ({_CID_SHORT}) · Gerrit Code Review\n"
        f"another change title ({_CID}) · Gerrit Code Review\n"
        "please @alice +1 and @bob +2"
    )
    assert extract_changes(text) == [
        f"change-id:{_CID_SHORT}",
        f"change-id:{_CID}",
    ]


def test_extract_bare_change_id_mixed_with_urls():
    text = f"https://gerrit.odm.example/c/p/+/55001 and ({_CID_SHORT})"
    assert extract_changes(text) == [
        "https://gerrit.odm.example/c/p/+/55001",
        f"change-id:{_CID_SHORT}",
    ]


def test_extract_bare_change_id_not_inside_url():
    # A /q/I<40> URL must be captured as the URL, NOT also emit a bare carrier.
    text = f"https://gerrit.odm.example/q/{_CID}"
    assert extract_changes(text) == [f"https://gerrit.odm.example/q/{_CID}"]


def test_extract_bare_change_id_dedup():
    text = f"({_CID_SHORT}) and again ({_CID_SHORT})"
    assert extract_changes(text) == [f"change-id:{_CID_SHORT}"]


def test_extract_bare_change_id_8_and_40_for_same_change():
    # Two different strings → two carriers at extract time; they collapse
    # later in resolve_change_ids() (both resolve to the same /c/N).
    text = f"({_CID_SHORT}) and full {_CID}"
    assert extract_changes(text) == [
        f"change-id:{_CID_SHORT}",
        f"change-id:{_CID}",
    ]


def test_extract_bare_change_id_rejects_non_hex():
    # x/y/z are not hex — no match.
    assert extract_changes("(I1234xyz0)") == []


def test_extract_bare_change_id_rejects_16_hex():
    # 16 hex is neither 8 nor 40 — no match.
    assert extract_changes("I1234567890abcdef") == []
