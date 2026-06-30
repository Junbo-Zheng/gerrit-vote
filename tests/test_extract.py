# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Junbo Zheng. All rights reserved.
"""Tests for change-link extraction and parsing."""

from gerrit_vote import extract_changes, parse_change


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
