<!-- SPDX-License-Identifier: Apache-2.0 -->
# gerrit-vote

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)
[![CI](https://github.com/Junbo-Zheng/gerrit-vote/actions/workflows/ci.yml/badge.svg)](https://github.com/Junbo-Zheng/gerrit-vote/actions/workflows/ci.yml)

Cast **label votes** on Gerrit changes from the command line, via the Gerrit
REST API — `Code-Review` by default, or any label your server exposes (`Verified`,
etc.). Copy a pile of change links from anywhere, run `gerrit-vote`, confirm, done.
Beyond voting it can also **abandon**, **restore**, and **delete** changes, and
**list** the changes you own or need to review.

> [!IMPORTANT]
> `gerrit-vote` **never submits or merges** a change. Voting is the default and
> only touches the `labels` field. The lifecycle actions (`--abandon`,
> `--restore`, `--delete`) change a change's state and are opt-in behind their
> own flags; `--delete` is irreversible and owner-only, and always asks first.

## Features

- **Clipboard-first.** A bare `gerrit-vote` reads the system clipboard, so you can
  copy a batch of links and just run the command. Pass explicit URLs to bypass the
  clipboard, or `--from-stdin` to read piped text.
- **Confirm before voting.** The changes are listed, then an arrow-key menu lets
  you pick the value. The value rows come from the server's permitted range for
  the change (so a `0`/`+1` label shows only `0`/`+1`, while `Code-Review` shows
  `+2`/`+1`/`0`/`-1`/`-2`), plus quit. `-y` skips it; `--dry-run` previews
  without voting.
- **Owner-only combo actions.** When the first change is yours (its owner email
  matches your `~/.gitconfig` `user.email`), the menu adds three one-shot combos
  above the plain rows — `Code-Review +1` together with `Presubmit-Ready +1`
  and/or `Auto-Submit +1`, cast in a single atomic review. They stay hidden on
  changes you don't own, and are all-or-nothing: if any label in the combo isn't
  permitted on a change, that change is skipped rather than partially voted.
- **Extracts links from messy text.** Finds Gerrit change links in pasted chat
  logs — de-duplicated, ignoring non-Gerrit noise, tolerating trailing CJK
  punctuation.
- **Topic batches.** Pass a `/q/topic:NAME` URL or `--topic NAME` to vote on every
  open change in a Gerrit topic; the topic is expanded to its changes before the
  confirmation prompt. Add `--related` to also pull in each change's dependency
  chain.
- **Multi-host batches.** Each change resolves its own host's credentials, so a
  mix of Gerrit instances votes each on the right one.
- **Lifecycle actions.** Beyond voting, `--abandon` / `--restore` / `--delete`
  act on the same change sources (URLs, clipboard, `--all`). All list the target
  changes and confirm via an arrow-key menu first. `--abandon` / `--restore` are
  reversible and default the menu to *Yes*; `--delete` is irreversible,
  **owner-only** (changes you don't own are skipped), and defaults to *No*.
- **List your changes.** `--list` prints a read-only table of your non-merged
  changes across every configured host — both those you own (open + abandoned)
  and those assigned to you for review, with review rows first.
- **Bulk cleanup.** `--all` sweeps every one of your changes across all hosts
  (`owner:self`) for a one-shot `--abandon` / `--restore` / `--delete`; it must
  be given explicitly and still runs the usual confirmation.
- **Skips closed changes.** Merged or abandoned changes are detected and skipped,
  never re-voted.
- **Permission fallback.** If `+2` is not in your permitted range but `+1` is, it
  downgrades automatically (disable with `--no-fallback`).
- **Transient-failure retry.** Server `5xx` and connection-level errors are
  retried a couple of times with a short backoff; deterministic `4xx` (bad auth,
  not permitted, no such change) fail fast without retry.
- **Readable output.** Colored results on a TTY (honoring `NO_COLOR`) plus a
  summary line every run.

## Installation

```bash
pip install gerrit-vote
```

Requires Python 3.10 or newer.

> [!TIP]
> Installing as a standalone CLI? `pipx install gerrit-vote` keeps it isolated
> from your other Python environments.

## Configuration

`gerrit-vote` needs a Gerrit HTTP password (not your login password) — generate
one at **Gerrit → Settings → HTTP Credentials → Generate Password**.

Credentials resolve in this order:

0. **Command-line overrides** `-u/--user` and `-p/--password` — win over env and
   config for that field, and apply to every host in the batch. Handy for CI or a
   throwaway identity. The URL still comes from the change URL / `GERRIT_URL` /
   `default_host`. Beware: a password on the command line lands in shell history.

1. **Environment variables** (single host):

   ```bash
   export GERRIT_URL=https://gerrit.example.com
   export GERRIT_USER=your-username
   export GERRIT_HTTP_PASSWORD=your-http-password
   ```

2. **Config file** `~/.config/gerrit-vote/config.toml` — required for multiple
   hosts. Copy [`config.toml.example`](config.toml.example) and fill it in:

   ```toml
   default_host = "gerrit.example.com"

   [hosts."gerrit.example.com"]
   url = "https://gerrit.example.com"
   user = "your-username"
   password = "your-http-password"
   ```

   ```bash
   mkdir -p ~/.config/gerrit-vote
   cp config.toml.example ~/.config/gerrit-vote/config.toml
   chmod 600 ~/.config/gerrit-vote/config.toml
   ```

> [!NOTE]
> If credentials are missing or a host is not configured, `gerrit-vote` prints the
> exact environment-variable and `config.toml` snippet you need — including the
> correct hostname — so you are never left guessing.

## Usage

The easiest flow — copy the links, then:

```bash
gerrit-vote          # read clipboard, list changes, pick +2/+1/0/-1/-2/quit
gerrit-vote 1        # same, but the menu defaults to +1
gerrit-vote -y       # skip the menu, vote +2 immediately
```

Vote on explicit changes:

```bash
# One change (clipboard ignored when URLs are given)
gerrit-vote https://gerrit.example.com/c/proj/+/12345

# Several on ONE line (space-separated), all +1
gerrit-vote <url1> <url2> <url3> 1

# With a review message
gerrit-vote https://gerrit.example.com/c/proj/+/12345 1 --message "LGTM"
```

Vote on every open change in a topic:

```bash
# Paste a topic query URL straight from the browser
gerrit-vote https://gerrit.example.com/q/topic:my-feature

# Or name the topic explicitly (uses the default/single configured host)
gerrit-vote --topic my-feature

# Also include each change's dependency chain
gerrit-vote --topic my-feature --related
```

Abandon, restore, or delete changes (same sources as voting — URLs, clipboard,
`--from-stdin`):

```bash
# Abandon (reversible); lists the changes, then an arrow-key menu (defaults to Yes)
gerrit-vote --abandon https://gerrit.example.com/c/proj/+/12345

# Restore an abandoned change (the inverse of --abandon)
gerrit-vote --restore https://gerrit.example.com/c/proj/+/12345

# Delete (IRREVERSIBLE, owner-only): lists, then an arrow-key menu defaulting to No
gerrit-vote --delete https://gerrit.example.com/c/proj/+/12345
gerrit-vote --delete --force-delete <url>   # skip the prompt (scripts/CI)
```

List your own and to-review changes, or act on all of them at once:

```bash
# Read-only table across every configured host (owned + assigned-for-review)
gerrit-vote --list

# Sweep ALL your open changes on every host and abandon them (one confirmation)
gerrit-vote --all --abandon

# Restore every one of your abandoned changes; delete every open one
gerrit-vote --all --restore
gerrit-vote --all --delete            # irreversible; still lists + confirms
```

> [!CAUTION]
> `--all` targets **every** matching change of yours across **all** hosts in one
> go. Preview first with `--all --abandon --dry-run` (or `--delete --dry-run`)
> before committing to a bulk action.

Override credentials on the command line (handy for CI or a throwaway identity;
the URL still comes from the change link / `GERRIT_URL` / `default_host`):

```bash
gerrit-vote https://gerrit.example.com/c/proj/+/12345 -u ci-bot -p "$GERRIT_PW"
```

Preview, or pipe text in:

```bash
gerrit-vote --dry-run            # list what would be voted, cast nothing
gerrit-vote --extract            # just print the change links found
pbpaste | gerrit-vote --from-stdin -y
```

> [!WARNING]
> Pasting multiple lines directly at the shell prompt does **not** work — the
> shell runs each line as its own command. Put several URLs on one line
> (space-separated), or use the clipboard / `--from-stdin`.

Closed changes are skipped automatically, results are colored on a TTY, and a
summary line is printed every run. Exit status is non-zero if any change failed.

### Options

| Option | Description |
| --- | --- |
| `[value]` | Vote value `2 1 0 -1 -2`; defaults to `+2` |
| `-c`, `--clipboard` | Read change links from the system clipboard |
| `--from-stdin` | Read change links from piped stdin |
| `--topic TOPIC` | Vote on every open change under a Gerrit topic (host = default/single configured host) |
| `--related` | Also vote on each change's dependency (relation) chain, de-duplicated |
| `--extract` | Only print the change links found; do not vote |
| `--dry-run` | Resolve credentials and read changes, but change nothing (also previews `--abandon`/`--restore`/`--delete`/`--all`) |
| `-y`, `--yes` | Skip the confirmation menu (votes immediately; also skips the abandon/restore/delete prompt) |
| `--abandon` | Abandon the change(s) instead of voting (reversible; MERGED skipped) |
| `--restore` | Restore abandoned change(s) — the inverse of `--abandon` (only ABANDONED changes) |
| `--delete` | Delete the change(s) — **irreversible**, owner-only; lists + confirms first |
| `--force-delete` | Skip the interactive confirm for `--delete` (for scripts; `-y` does the same) |
| `--all` | Target ALL your changes across every configured host (`owner:self`); requires `--abandon`/`--restore`/`--delete` |
| `--list` | List your non-merged changes (owned + to-review) across all hosts, then exit; changes nothing |
| `--label LABEL` | Label to vote on (default `Code-Review`; common: `Code-Review`, `Verified`) |
| `--list-labels CHANGE` | Print the labels you may vote on `CHANGE` (name + allowed values), then exit; casts nothing |
| `-m`, `--message MSG` | Review message posted alongside the vote |
| `-u`, `--user USER` | Gerrit username override (else env/config); applies to every host |
| `-p`, `--password PW` | Gerrit HTTP password override (else env/config); visible in shell history |
| `--no-fallback` | Disable the `+2 → +1` downgrade when `+2` is not permitted |
| `-V`, `--version` | Print the version and exit |

### Labels

`Code-Review` is the default and, on most servers, the only label a human votes
by hand. But the available labels are **defined per Gerrit server** (a server may
also expose `Verified`, `Owners-Override`, and any number of CI/bot labels), and
**which ones you may vote on a given change** is decided per-change by your
project/branch permissions. Rather than hardcode a list that would be wrong on
another server, query it live:

```console
$ gerrit-vote --list-labels https://gerrit.example.com/c/12345
Labels you may vote on this change:
  Code-Review: -2, -1, 0, +1, +2
  Verified: -1, 0, +1
```

Then pass one to `--label`:

```console
$ gerrit-vote --label Verified https://gerrit.example.com/c/12345 1
```

### Combo actions on your own changes

When a change is yours, the interactive menu offers three one-shot combos above
the usual value rows, each casting several labels in a single review:

```text
❯ 1. Code-Review +1, Presubmit-Ready +1, Auto-Submit +1
  2. Code-Review +1, Presubmit-Ready +1
  3. Code-Review +1, Auto-Submit +1
  4. +2  approve
  5. +1  looks good
  ...
```

Ownership is decided by comparing the change's owner email with your global git
identity (`git config --global user.email`, i.e. `~/.gitconfig`), so a repo-local
override does not shadow it. Combos are hidden on changes you don't own. They are
also all-or-nothing per change: if the server doesn't permit every label in the
chosen combo on a given change (e.g. that project has no `Auto-Submit`), the
change is skipped rather than partially voted.

## Library use

The voting and extraction logic is importable:

```python
from gerrit_vote import extract_changes, vote

for url in extract_changes(pasted_text):
    result = vote(url)  # defaults to +2, never submits
    if result.skipped:
        print(result.change_id, "skipped:", result.skip_reason)
    else:
        print(result.change_id, "->", result.cast)  # may be 1 after fallback
```

Expand a topic into its open changes, then vote on each:

```python
from gerrit_vote import changes_in_topic, resolve, vote

creds = resolve("gerrit.example.com")  # or None for the default host
for url in changes_in_topic(creds, "my-feature"):
    vote(url, creds=creds)
```

Lifecycle actions and listing are importable too. `abandon` / `restore` return a
`VoteResult` (with `.action` set); `delete_change` takes an owner gate; and
`list_my_changes` returns `ChangeListItem`s tagged `is_owner`:

```python
from gerrit_vote import abandon, restore, delete_change, list_my_changes, resolve

abandon("https://gerrit.example.com/c/proj/+/12345")   # reversible
restore("https://gerrit.example.com/c/proj/+/12345")   # inverse of abandon

# Delete only your own change (require_owner_email gates it; None disables the gate)
delete_change("https://gerrit.example.com/c/proj/+/12345",
              require_owner_email="me@example.com")

creds = resolve("gerrit.example.com")
for item in list_my_changes(creds):
    tag = "mine" if item.is_owner else "REVIEW"
    print(item.number, item.status, tag, item.subject)
```

## Development

Run straight from the source tree — no build, no install:

```bash
./main.py --dry-run <url>   # the CLI, imported from src/ on the fly
pytest                      # tests import from src/ via pythonpath, no install needed
```

`pyproject.toml` sets `pythonpath = ["src"]`, so `pytest` runs against the working
tree directly and always shadows any installed copy. `./main.py` is a thin shim
that does the same for the CLI. Install the dev tools once with:

```bash
pip install ".[dev]" --no-deps   # pytest + black; the package itself need not be installed
```

CI runs on every push and pull request: a fast job lints with `black` and runs the
suite against `src/`, plus a `package` job that builds the wheel, installs it
clean, and re-runs the suite with `pythonpath` cleared — so the *installed* package
is exercised and packaging bugs fail the build.

## Releasing

Publishing is automated: cut a **GitHub Release** and `publish.yml` builds the
distribution and uploads it to PyPI via Trusted Publishing (OIDC — no API token).
The repo must be registered as a trusted publisher on the PyPI project once. Bump
`__version__` in `src/gerrit_vote/__init__.py` before tagging — PyPI is
append-only.
