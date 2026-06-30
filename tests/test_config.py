# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Junbo Zheng. All rights reserved.
"""Tests for credential resolution, especially multi-host config."""

import textwrap

import pytest

from gerrit_vote import ConfigError, resolve
from gerrit_vote import _config


@pytest.fixture
def clean_env(monkeypatch):
    for k in (
        "GERRIT_URL",
        "GERRIT_USER",
        "GERRIT_USERNAME",
        "GERRIT_HTTP_PASSWORD",
        "GERRIT_PASSWORD",
        "GERRIT_VOTE_CONFIG",
        "XDG_CONFIG_HOME",
    ):
        monkeypatch.delenv(k, raising=False)


def _write_config(tmp_path, body):
    p = tmp_path / "config.toml"
    p.write_text(textwrap.dedent(body))
    return p


def test_env_single_host(clean_env, monkeypatch):
    monkeypatch.setenv("GERRIT_URL", "https://gerrit.a.example/")
    monkeypatch.setenv("GERRIT_USER", "alice")
    monkeypatch.setenv("GERRIT_HTTP_PASSWORD", "secret")
    c = resolve("gerrit.a.example")
    assert c.user == "alice"
    assert c.url == "https://gerrit.a.example"  # trailing slash stripped


def test_multi_host_config_resolves_each(clean_env, monkeypatch, tmp_path):
    cfg = _write_config(
        tmp_path,
        """
        [hosts."gerrit.pt.example"]
        url = "https://gerrit.pt.example"
        user = "pt-user"
        password = "pt-pw"

        [hosts."gerrit.odm.example"]
        url = "https://gerrit.odm.example"
        user = "odm-user"
        password = "odm-pw"
        """,
    )
    monkeypatch.setenv("GERRIT_VOTE_CONFIG", str(cfg))

    pt = resolve("gerrit.pt.example")
    odm = resolve("gerrit.odm.example")
    assert (pt.user, odm.user) == ("pt-user", "odm-user")
    assert pt.url == "https://gerrit.pt.example"
    assert odm.url == "https://gerrit.odm.example"


def test_bare_number_ambiguous_multi_host(clean_env, monkeypatch, tmp_path):
    cfg = _write_config(
        tmp_path,
        """
        [hosts."gerrit.pt.example"]
        user = "a"
        password = "x"

        [hosts."gerrit.odm.example"]
        user = "b"
        password = "y"
        """,
    )
    monkeypatch.setenv("GERRIT_VOTE_CONFIG", str(cfg))
    with pytest.raises(ConfigError, match="multiple Gerrit hosts"):
        resolve(None)


def test_bare_number_default_host(clean_env, monkeypatch, tmp_path):
    cfg = _write_config(
        tmp_path,
        """
        default_host = "gerrit.pt.example"

        [hosts."gerrit.pt.example"]
        user = "a"
        password = "x"

        [hosts."gerrit.odm.example"]
        user = "b"
        password = "y"
        """,
    )
    monkeypatch.setenv("GERRIT_VOTE_CONFIG", str(cfg))
    assert resolve(None).user == "a"


def test_unknown_host_errors(clean_env, monkeypatch, tmp_path):
    cfg = _write_config(
        tmp_path,
        """
        [hosts."gerrit.pt.example"]
        user = "a"
        password = "x"
        """,
    )
    monkeypatch.setenv("GERRIT_VOTE_CONFIG", str(cfg))
    with pytest.raises(ConfigError, match="No credentials for host"):
        resolve("gerrit.unknown.example")


def test_user_password_override_no_stored_creds(clean_env):
    # With both overrides and a host from the change URL, no env/config needed.
    c = resolve("gerrit.z.example", user="cli-user", password="cli-pw")
    assert (c.user, c.password) == ("cli-user", "cli-pw")
    assert c.url == "https://gerrit.z.example"


def test_user_override_keeps_config_url_and_password(clean_env, monkeypatch, tmp_path):
    # --user alone overrides just the username; URL + password come from config.
    cfg = _write_config(
        tmp_path,
        """
        [hosts."gerrit.pt.example"]
        url = "https://gerrit.pt.example:8443"
        user = "stored-user"
        password = "stored-pw"
        """,
    )
    monkeypatch.setenv("GERRIT_VOTE_CONFIG", str(cfg))
    c = resolve("gerrit.pt.example", user="cli-user")
    assert c.user == "cli-user"
    assert c.password == "stored-pw"
    assert c.url == "https://gerrit.pt.example:8443"  # custom port preserved


def test_password_only_without_stored_user_errors(clean_env):
    with pytest.raises(ConfigError, match="Incomplete credentials"):
        resolve("gerrit.z.example", password="cli-pw")


def test_override_without_resolvable_url_errors(clean_env, monkeypatch, tmp_path):
    # Bare number (no host), both overrides, but no URL source at all.
    # Point config at a nonexistent path so no default_host/url can be found.
    monkeypatch.setenv("GERRIT_VOTE_CONFIG", str(tmp_path / "nope.toml"))
    with pytest.raises(ConfigError, match="Cannot determine the Gerrit URL"):
        resolve(None, user="cli-user", password="cli-pw")


# ---------------------------------------------------------------------------
# all_hosts() — used by the CLI's --all sweep
# ---------------------------------------------------------------------------


def test_all_hosts_lists_every_config_host(clean_env, monkeypatch, tmp_path):
    from gerrit_vote import all_hosts

    cfg = _write_config(
        tmp_path,
        """
        [hosts."gerrit.pt.example"]
        url = "https://gerrit.pt.example"
        user = "alice"
        password = "pw-pt"

        [hosts."gerrit.odm.example"]
        url = "https://gerrit.odm.example"
        user = "alice"
        password = "pw-odm"
        """,
    )
    monkeypatch.setenv("GERRIT_VOTE_CONFIG", str(cfg))
    hosts = {c.host: c for c in all_hosts()}
    assert set(hosts) == {"gerrit.pt.example", "gerrit.odm.example"}
    assert hosts["gerrit.odm.example"].password == "pw-odm"


def test_all_hosts_merges_env_and_file(clean_env, monkeypatch, tmp_path):
    from gerrit_vote import all_hosts

    cfg = _write_config(
        tmp_path,
        """
        [hosts."gerrit.pt.example"]
        url = "https://gerrit.pt.example"
        user = "alice"
        password = "pw-pt"
        """,
    )
    monkeypatch.setenv("GERRIT_VOTE_CONFIG", str(cfg))
    monkeypatch.setenv("GERRIT_URL", "https://gerrit.env.example")
    monkeypatch.setenv("GERRIT_USER", "bob")
    monkeypatch.setenv("GERRIT_HTTP_PASSWORD", "pw-env")
    hosts = {c.host for c in all_hosts()}
    assert hosts == {"gerrit.pt.example", "gerrit.env.example"}


def test_all_hosts_applies_overrides(clean_env, monkeypatch, tmp_path):
    from gerrit_vote import all_hosts

    cfg = _write_config(
        tmp_path,
        """
        [hosts."gerrit.pt.example"]
        url = "https://gerrit.pt.example"
        user = "alice"
        password = "pw-pt"
        """,
    )
    monkeypatch.setenv("GERRIT_VOTE_CONFIG", str(cfg))
    hosts = all_hosts(user="cli-user", password="cli-pw")
    assert all(c.user == "cli-user" and c.password == "cli-pw" for c in hosts)
    assert hosts[0].url == "https://gerrit.pt.example"  # url kept


def test_all_hosts_empty_raises(clean_env, monkeypatch, tmp_path):
    from gerrit_vote import all_hosts

    monkeypatch.setenv("GERRIT_VOTE_CONFIG", str(tmp_path / "nope.toml"))
    with pytest.raises(ConfigError):
        all_hosts()
