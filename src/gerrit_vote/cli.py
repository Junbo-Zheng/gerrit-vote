# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Junbo Zheng. All rights reserved.
"""Command-line interface for gerrit-vote.

Entry styles:

  gerrit-vote                     # read the clipboard, confirm, then vote
  gerrit-vote [value]             # same, with an explicit vote value
  gerrit-vote <url1> <url2> ...   # vote on the URLs given (clipboard ignored)
  gerrit-vote --from-stdin        # read piped text instead of the clipboard
  gerrit-vote --extract           # only print the change links, do not vote
  gerrit-vote --abandon <url>     # abandon (reversible); --restore is the inverse
  gerrit-vote --delete <url>      # delete (irreversible, owner-only)
  gerrit-vote --list              # list your owned + to-review changes, then exit
  gerrit-vote --all --abandon     # sweep all your open changes on every host
  gerrit-vote --all --delete      # sweep open + abandoned (menu picks which)

Source resolution: explicit URL arguments win; with no URLs, a bare invocation
reads the system clipboard (--from-stdin reads piped text instead; --all sweeps
owner:self across every configured host). Before anything is cast or changed the
targets are listed and confirmed via an arrow-key menu, unless -y is given or it
is a --dry-run. The default vote value is +2. Hosts are resolved per change URL,
so a batch mixing multiple Gerrit hosts acts each on the correct host.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass

from .config import ConfigError, all_hosts, resolve
from .core import (
    ChangeMeta,
    VoteError,
    VoteResult,
    abandon,
    change_meta,
    change_topic,
    changes_in_topic,
    dedup_changes,
    delete_change,
    extract_changes,
    extract_topics,
    fmt,
    git_identity_email,
    list_my_changes,
    my_changes,
    parse_change,
    parse_topic,
    permitted_labels,
    related_changes,
    resolve_change_ids,
    restore,
    vote,
    vote_labels,
)


def _read_clipboard() -> str:
    """Return the system clipboard text, trying common tools in order.

    Supports Linux X11 (xclip/xsel), Wayland (wl-paste), and macOS (pbpaste).
    Raises VoteError with install hints if no usable tool is found.
    """
    candidates = [
        (["xclip", "-o", "-selection", "clipboard"], "xclip"),
        (["xsel", "-b", "-o"], "xsel"),
        (["wl-paste", "--no-newline"], "wl-paste"),
        (["pbpaste"], "pbpaste"),
    ]
    for cmd, _tool in candidates:
        if shutil.which(cmd[0]):
            try:
                out = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
                if out.returncode == 0:
                    return out.stdout
            except (subprocess.SubprocessError, OSError):
                continue
    raise VoteError(
        "No clipboard tool found. Install one of: xclip / xsel (X11), "
        "wl-clipboard (Wayland), or use pbpaste (macOS). "
        "Alternatively pipe text in with --from-stdin."
    )


_QUIT = object()  # sentinel returned when the user chooses to quit

# ANSI colors, applied only when stdout is a real terminal and NO_COLOR is
# unset (https://no-color.org). Otherwise _c() returns the text unchanged so
# piped/redirected output stays clean.
_ANSI = {
    "green": "\033[32m",
    "yellow": "\033[33m",
    "red": "\033[31m",
    "dim": "\033[2m",
    "bold": "\033[1m",
}
_RESET = "\033[0m"


def _color_enabled() -> bool:
    return sys.stdout.isatty() and "NO_COLOR" not in os.environ


def _c(text: str, color: str) -> str:
    if not _color_enabled():
        return text
    return f"{_ANSI.get(color, '')}{text}{_RESET}"


# Human-readable hint per vote value, shown next to the number in the menu.
# Values the server permits but that are outside this set (rare CI/bot labels)
# render as the bare number with no hint.
_VALUE_DESC = {
    2: "approve",
    1: "looks good",
    0: "no score",
    -1: "prefer not",
    -2: "do not merge",
}
# Fallback menu values when the server's permitted set can't be fetched.
_DEFAULT_VALUES = [2, 1, 0, -1, -2]


# Owner-only combo actions, offered above the plain +2..-2 rows ONLY when the
# change is owned by the caller (git identity == change owner). Each casts
# several labels in one atomic POST via vote_labels(). Order = menu order.
@dataclass(frozen=True)
class _Combo:
    title: str
    labels: dict[str, int]


_COMBO_ACTIONS = [
    _Combo(
        "Code-Review +1, Presubmit-Ready +1, Auto-Submit +1",
        {"Code-Review": 1, "Presubmit-Ready": 1, "Auto-Submit": 1},
    ),
    _Combo(
        "Code-Review +1, Presubmit-Ready +1",
        {"Code-Review": 1, "Presubmit-Ready": 1},
    ),
    _Combo(
        "Code-Review +1, Auto-Submit +1",
        {"Code-Review": 1, "Auto-Submit": 1},
    ),
]


def _menu_fmt(value: int) -> str:
    """Format a vote value padded to 2 chars so the menu column aligns.

    Unlike core.fmt (which gives "0"), zero renders as " 0" to line up with
    "+2"/"-1", matching the original hand-written menu layout.
    """
    return " 0" if value == 0 else f"{value:+d}"


def _menu_values(permitted: dict[str, list[str]] | None, label: str) -> list[int]:
    """Return the vote values to offer for ``label``, newest-first (descending).

    ``permitted`` is a permitted_labels map — in practice the UNION of every
    change's map in the batch (built by the caller), so a 0/+1 label
    (Presubmit-Ready, Auto-Submit) shows only 0/+1 while Code-Review still shows
    -2..+2, and the menu's top reaches the highest value any change permits. When
    it is None (meta fetch failed for every change) or the label is absent, fall
    back to the full -2..+2 set; core.vote() still validates each change, so an
    out-of-range pick is caught rather than mis-cast.
    """
    allowed = (permitted or {}).get(label)
    if allowed:
        try:
            values = sorted({int(v.strip()) for v in allowed}, reverse=True)
            if values:
                return values
        except ValueError:
            pass
    return _DEFAULT_VALUES


def _pick_delete_scope():
    """Ask which of your changes ``--all --delete`` should sweep.

    Returns ``"open"`` / ``"abandoned"`` / ``"all"`` (open + abandoned), or
    ``_QUIT`` on cancel. Only the delete action meaningfully offers a choice —
    abandon only applies to open changes, restore only to abandoned ones, so
    their sweep status stays fixed by the action. Non-TTY callers and ``-y``
    never reach here; the --all block falls back to ``"open"``
    (backwards-compatible) in those cases.
    """
    import questionary
    from questionary import Choice

    choices = [
        Choice(title="1. my open changes", value="open"),
        Choice(title="2. my abandoned changes", value="abandoned"),
        Choice(title="3. both open + abandoned", value="all"),
        Choice(title="4. quit", value=_QUIT),
    ]
    question = questionary.select(
        "Delete which of your changes?",
        choices=choices,
        default=None,  # "open" focused — matches the pre-choice behavior
        pointer="❯",
        instruction="(arrow keys; Esc or Ctrl-C to cancel)",
    )
    # Bind Esc to cancel, matching the other menus.
    kb = question.application.key_bindings
    if kb is not None:

        @kb.add("escape", eager=True)  # type: ignore[attr-defined]
        def _esc(event):  # noqa: ANN001 (prompt_toolkit event)
            event.app.exit(result=None)

    choice = question.ask()
    return _QUIT if choice is None else choice


def _is_owned_by_me(owner_email: str) -> bool:
    """True when the change owner's email matches the local git identity.

    Identity comes from ~/.gitconfig (git config --global user.email), compared
    case-insensitively. Any missing piece (no git email, no owner email) is a
    conservative False, so combo actions stay hidden unless ownership is proven.
    """
    mine = git_identity_email()
    return bool(mine and owner_email and owner_email.lower() == mine.lower())


def _confirm_and_pick_value(
    urls: list[str], label: str, user: str | None = None, password: str | None = None
):
    """List the changes, then let the user pick the vote via arrow keys.

    Value rows come from the UNION of permitted values across every change: the
    plain +2..-2 values permitted for ``label`` on ANY change (highest first),
    always shown — so a batch whose first change is +1-only does not cap the
    menu at +1. When the first change is owned by the caller (git identity ==
    owner), three owner-only combo actions are added ABOVE the plain rows.
    Returns one of:
      * an int   -> single-label vote of that value on ``label``
      * a dict   -> multi-label combo, e.g. {"Code-Review": 1, "Auto-Submit": 1}
      * _QUIT    -> abort (also returned when stdin is not a TTY)
    """
    # Fetch metadata for every change up front so the header can show each
    # change's subject / owner / author before any vote — you should see who
    # you are voting on, not just a bare URL. The union of all metas drives the
    # value rows (permitted); the first change's metadata drives the owner-only
    # combo gate.
    metas: list[ChangeMeta | None] = []
    for u in urls:
        try:
            metas.append(change_meta(u, user=user, password=password))
        except (VoteError, ConfigError):
            metas.append(None)  # keep positions aligned with urls

    print(f"About to vote {label} on {len(urls)} change(s):")
    for u, m in zip(urls, metas):
        print(f"  {u}")
        if m is not None:
            # People tag trails the subject on the same line, like the summary's
            # [email]: owner alone when author matches, else owner + author.
            owner = m.owner_email or "(unknown)"
            if m.author_email and m.author_email != m.owner_email:
                who = f"{owner} / author {m.author_email}"
            else:
                who = owner
            subject = _c(m.subject or "(no subject)", "bold")
            print(f"     {subject}  {_c(f'[{who}]', 'dim')}".rstrip())

    if not sys.stdin.isatty():
        print("stdin is not a TTY; cannot show the menu. Re-run with -y to proceed.")
        return _QUIT

    # The first change's metadata drives whether to offer combo actions (owner
    # vs the local git identity). The value rows, however, come from the UNION
    # of permitted values across EVERY change in the batch — not just the
    # first. Otherwise a batch whose first change is +1-only caps the menu at
    # +1, and even +2-capable changes end up +1 (vote() only downgrades +2->+1
    # per change at execution; it never upgrades). With the union, the menu's
    # top is the highest value permitted on ANY change; per-change fallback in
    # vote() then downgrades +2->+1 where +2 isn't permitted. None everywhere
    # => meta fetch failed, fall back to plain -2..+2 rows with no combos.
    first = metas[0]
    owned_by_me = _is_owned_by_me(first.owner_email) if first is not None else False

    union_vals: list[str] = []
    for m in metas:
        if m is not None:
            union_vals.extend((m.permitted or {}).get(label, []))
    union_permit = {label: union_vals} if union_vals else None

    import questionary
    from questionary import Choice

    # One line per option: a numbered title, no separate description row (the
    # title already says what it casts). The pointer (set below) marks the row.
    choices = []
    idx = 1

    # Owner-only combo actions, above the plain rows. Only shown when the change
    # is proven to be the caller's own — the point of the feature.
    if owned_by_me:
        for combo in _COMBO_ACTIONS:
            choices.append(Choice(title=f"{idx}. {combo.title}", value=combo.labels))
            idx += 1

    # Plain single-label value rows, built from the union of server-permitted
    # values so the menu never offers an impossible vote on every change, yet
    # still reaches the highest value any change in the batch allows.
    for value in _menu_values(union_permit, label):
        desc = _VALUE_DESC.get(value, "")
        title = f"{idx}. {_menu_fmt(value)}  {desc}".rstrip()
        choices.append(Choice(title=title, value=value))
        idx += 1

    choices.append(Choice(title=f"{idx}. quit", value=_QUIT))
    question = questionary.select(
        f"Set {label} to:",
        choices=choices,
        default=None,  # first choice (+2) is focused by default
        pointer="❯",  # the Claude-style caret
        instruction="(arrow keys; Esc or Ctrl-C to cancel)",
    )

    # questionary 2.x does not bind Esc on select by default (only Ctrl-C/Ctrl-Q
    # abort). Register Esc explicitly so it cancels like choosing quit — exiting
    # with None, which we map to _QUIT below.
    kb = question.application.key_bindings
    if kb is not None:
        # KeyBindingsBase is abstract in typeshed; the concrete instance has .add.
        @kb.add("escape", eager=True)  # type: ignore[attr-defined]
        def _esc(event):  # noqa: ANN001 (prompt_toolkit event)
            event.app.exit(result=None)

    choice = question.ask()

    # ask() returns None on Esc / Ctrl-C -> treat as quit.
    return _QUIT if choice is None else choice


def _is_value(token: str) -> bool:
    """True if a positional token is a vote value, not a change number.

    Vote labels only range -2..+2, so a small signed integer in that set is
    treated as the value; a large bare integer (e.g. 9182632) is a change.
    """
    try:
        return int(token) in (-2, -1, 0, 1, 2)
    except ValueError:
        return False


def _vote_desc(r: VoteResult) -> str:
    """Human-readable description of what was (or would be) cast on ``r``.

    A combo result (multiple labels) renders as "Code-Review+1, Auto-Submit+1";
    a single-label result renders as "Code-Review=+2", matching the classic
    output. The cast/requested distinction is preserved for single-label votes.
    """
    if r.labels and len(r.labels) > 1:
        return ", ".join(f"{n}{fmt(v)}" for n, v in r.labels.items())
    return f"{r.label}={fmt(r.cast if r.cast is not None else r.requested)}"


def _print_result(r: VoteResult) -> None:
    head = (
        f"Change {r.change_id} [{r.status}] on {r.host}\n"
        f"  subject: {r.subject}\n"
        f"  owner:  {r.owner_email or '(unknown)'}\n"
        f"  author: {r.author_email or '(unknown)'}\n"
        f"  project: {r.project}  branch: {r.branch}"
    )
    print(head)
    # abandon / restore / delete take separate wording — no vote value.
    if r.action in ("abandon", "restore", "delete"):
        _print_lifecycle_result(r)
        return
    if r.skipped:
        print(_c(f"  SKIP: {r.skip_reason}; no vote sent.", "yellow"))
        return
    if r.downgraded:
        print(_c("  note: +2 not permitted; fell back to +1.", "yellow"))
    if r.dry_run:
        print(
            _c(
                f"  [dry-run] would set {_vote_desc(r)} (NO submit)",
                "dim",
            )
        )
    else:
        print(
            _c(
                f"  OK: {_vote_desc(r)} cast on {r.change_id}. (not submitted)",
                "green",
            )
        )


# action -> (verb for dry-run/skip wording, DONE word for success line)
_LIFECYCLE_WORDS = {
    "abandon": ("abandon", "ABANDONED"),
    "restore": ("restore", "RESTORED"),
    "delete": ("delete", "DELETED"),
}


def _print_lifecycle_result(r: VoteResult) -> None:
    """Print an abandon/restore/delete result (no vote value, distinct verbs)."""
    verb, done = _LIFECYCLE_WORDS[r.action]
    if r.skipped:
        print(_c(f"  SKIP: {r.skip_reason}; {verb} not done.", "yellow"))
        return
    if r.dry_run:
        print(_c(f"  [dry-run] would {verb} change {r.change_id}", "dim"))
        return
    print(_c(f"  OK: {done} {r.change_id}.", "green"))


def _print_summary(
    results: list[VoteResult],
    errors: list[tuple[str, str]],
    action: str | None = None,
) -> None:
    """Print a per-change summary table so the outcome is scannable at a glance.

    ``action`` names the operation ("vote"/"abandon"/"restore"/"delete") so the
    tally line reads right even when EVERY change failed (results empty). It is
    passed by the lifecycle callers; when omitted it is inferred from the first
    result, defaulting to "vote".
    """
    print(_c("==== summary ====", "bold"))
    # All results in one call share the same action (the CLI dispatches per
    # action). Prefer the explicit action, else infer from the first result.
    if action is None:
        action = results[0].action if results else "vote"
    for r in results:
        if r.skipped:
            outcome = _c(f"SKIPPED ({r.skip_reason})", "yellow")
        elif r.action in _LIFECYCLE_WORDS:
            verb, done = _LIFECYCLE_WORDS[r.action]
            outcome = (
                _c(f"dry-run -> would {verb}", "dim")
                if r.dry_run
                else _c(f"OK {done}", "green")
            )
        elif r.dry_run:
            outcome = _c(f"dry-run -> would be {_vote_desc(r)}", "dim")
        elif r.downgraded:
            outcome = _c(f"OK {r.label}=+1 (fell back from +2)", "yellow")
        else:
            outcome = _c(f"OK {_vote_desc(r)}", "green")
        owner = _c(f"[{r.owner_email}]", "dim") if r.owner_email else ""
        print(f"  {r.change_id:<10} {outcome}  {owner}".rstrip())
    for ch, msg in errors:
        first_line = msg.splitlines()[0] if msg else "error"
        print(f"  {ch:<10} {_c(f'FAILED ({first_line})', 'red')}")

    acted = sum(1 for r in results if not r.skipped and not r.dry_run)
    downgraded = sum(1 for r in results if r.downgraded)
    skipped = sum(1 for r in results if r.skipped)
    done_word = {
        "abandon": "abandoned",
        "restore": "restored",
        "delete": "deleted",
    }.get(action, "voted")
    parts = [_c(f"{acted} {done_word}", "green")]
    if downgraded:
        parts.append(_c(f"{downgraded} downgraded to +1", "yellow"))
    if skipped:
        parts.append(_c(f"{skipped} skipped", "yellow"))
    if errors:
        parts.append(_c(f"{len(errors)} failed", "red"))
    print("---- " + ", ".join(parts) + " ----")


def _vote_many(
    changes: list[str],
    value: int | dict[str, int],
    label: str,
    message: str | None,
    dry_run: bool,
    fallback: bool,
    user: str | None = None,
    password: str | None = None,
) -> int:
    # A dict value is an owner-only combo (multi-label atomic vote); an int is a
    # plain single-label vote. Combos are gated on ownership per change, so grab
    # the local git identity once to enforce it in vote_labels().
    is_combo = isinstance(value, dict)
    owner_guard = git_identity_email() if is_combo else None

    rc = 0
    results: list[VoteResult] = []
    errors: list[tuple[str, str]] = []
    for ch in changes:
        try:
            if is_combo:
                r = vote_labels(
                    ch,
                    value,  # type: ignore[arg-type]
                    message=message,
                    dry_run=dry_run,
                    require_owner_email=owner_guard,
                    user=user,
                    password=password,
                )
            else:
                r = vote(
                    ch,
                    value,  # type: ignore[arg-type]
                    label=label,
                    message=message,
                    dry_run=dry_run,
                    fallback=fallback,
                    user=user,
                    password=password,
                )
            _print_result(r)
            results.append(r)
        except (VoteError, ConfigError) as e:
            print(f"error [{ch}]: {e}", file=sys.stderr)
            errors.append((ch, str(e)))
            rc = 1
        print("----")
    # Always print the summary, for one change or many, so the outcome line
    # (voted / skipped / failed, colored) is consistent regardless of count.
    _print_summary(results, errors)
    return rc


def _confirm_action(
    urls: list[str], verb: str, user: str | None = None, password: str | None = None
) -> bool:
    """List the changes, then confirm ``verb`` via an arrow-key select menu.

    Same UX as the vote value menu (questionary.select, arrow keys + Enter).
    The default-focused row depends on how destructive ``verb`` is:
      * delete (irreversible) -> "No" is first, so a reflexive Enter cancels.
      * abandon / restore (reversible) -> "Yes" is first, so Enter proceeds.
    Returns True only on an explicit Yes. Non-TTY stdin returns False (a
    destructive action never runs unattended).
    """
    print(f"About to {verb} {len(urls)} change(s):")
    for u in urls:
        try:
            m = change_meta(u, user=user, password=password)
        except (VoteError, ConfigError):
            m = None
        print(f"  {u}")
        if m is not None:
            owner = m.owner_email or "(unknown)"
            subject = _c(m.subject or "(no subject)", "bold")
            print(f"     {subject}  {_c(f'[{owner}]', 'dim')}".rstrip())

    if not sys.stdin.isatty():
        print("stdin is not a TTY; refusing to proceed without confirmation.")
        return False

    import questionary
    from questionary import Choice

    yes_label = f"Yes, {verb} {len(urls)} change(s)"
    no_choice = Choice(title="No, cancel", value=False)
    if verb == "delete":
        # Irreversible: "No" first so it holds the default focus — a reflexive
        # Enter cancels rather than deletes.
        yes_label += " — irreversible"
        choices = [no_choice, Choice(title=yes_label, value=True)]
    else:
        # Reversible (abandon/restore): "Yes" first so a reflexive Enter runs
        # the action, per the user's requested ergonomics.
        choices = [Choice(title=yes_label, value=True), no_choice]
    question = questionary.select(
        f"{verb.capitalize()} these change(s)?",
        choices=choices,
        default=None,  # first choice focused (No for delete, Yes otherwise)
        pointer="❯",
        instruction="(arrow keys; Esc or Ctrl-C to cancel)",
    )

    # Bind Esc to cancel, matching the vote menu.
    kb = question.application.key_bindings
    if kb is not None:

        @kb.add("escape", eager=True)  # type: ignore[attr-defined]
        def _esc(event):  # noqa: ANN001 (prompt_toolkit event)
            event.app.exit(result=None)

    choice = question.ask()
    # ask() returns None on Esc / Ctrl-C -> treat as cancel.
    return choice is True


def _lifecycle_many(
    changes: list[str],
    action,  # abandon or restore — same signature
    message: str | None,
    dry_run: bool,
    user: str | None = None,
    password: str | None = None,
) -> int:
    """Apply a lifecycle ``action`` (abandon/restore) to each change.

    abandon() and restore() share the same signature and return a VoteResult
    tagged with their ``action``, so one loop drives both — the display layer
    (_print_result / _print_summary) branches on ``r.action``.
    """
    rc = 0
    results: list[VoteResult] = []
    errors: list[tuple[str, str]] = []
    for ch in changes:
        try:
            r = action(
                ch, message=message, dry_run=dry_run, user=user, password=password
            )
            _print_result(r)
            results.append(r)
        except (VoteError, ConfigError) as e:
            print(f"error [{ch}]: {e}", file=sys.stderr)
            errors.append((ch, str(e)))
            rc = 1
        print("----")
    # action.__name__ is "abandon" / "restore" — pass it so the tally line is
    # right even if every change failed (results empty).
    _print_summary(results, errors, action=getattr(action, "__name__", None))
    return rc


def _delete_many(
    changes: list[str],
    dry_run: bool,
    user: str | None = None,
    password: str | None = None,
) -> int:
    # Owner-only: delete is restricted to the caller's own changes. The local
    # git identity is the gate; a change owned by anyone else is skipped (no
    # request sent), mirroring the combo owner_guard in _vote_many().
    owner_guard = git_identity_email()
    rc = 0
    results: list[VoteResult] = []
    errors: list[tuple[str, str]] = []
    for ch in changes:
        try:
            r = delete_change(
                ch,
                require_owner_email=owner_guard,
                dry_run=dry_run,
                user=user,
                password=password,
            )
            _print_result(r)
            results.append(r)
        except (VoteError, ConfigError) as e:
            print(f"error [{ch}]: {e}", file=sys.stderr)
            errors.append((ch, str(e)))
            rc = 1
        print("----")
    _print_summary(results, errors, action="delete")
    return rc


def _delete_flow(
    urls: list[str],
    *,
    dry_run: bool,
    force: bool,
    user: str | None = None,
    password: str | None = None,
) -> int:
    """Shared delete flow: confirm (arrow-key menu) -> delete.

    Branches:
      * ``dry_run`` (--dry-run): print the full dry-run preview and stop. This
        is the only path that prints the preview + summary, since it is the one
        the user asked for a preview.
      * ``force`` (--force-delete / -y): delete without prompting, for
        scripted/non-interactive use.
      * otherwise: confirm via the arrow-key select menu (_confirm_action),
        which lists the changes AND asks — so no separate preview is printed
        (that was redundant). The menu refuses a non-TTY, so nothing is
        blind-deleted.

    Used by both a plain ``--delete`` and ``--all --delete``.
    """
    if dry_run:
        print(_c("Delete preview (dry-run — nothing deleted yet):", "bold"))
        return _delete_many(urls, dry_run=True, user=user, password=password)
    if not force:
        if not _confirm_action(urls, "delete", user=user, password=password):
            print("Aborted; nothing deleted.")
            return 1
    return _delete_many(urls, dry_run=False, user=user, password=password)


def _list_my_changes(user: str | None = None, password: str | None = None) -> int:
    """Print your non-merged changes (owned + review requests) as a table.

    Sweeps every configured host and prints one table per host with columns
    ID | STATUS | REL | URL | REPO | BRANCH | SUBJECT, where REL is ``owner``
    (you pushed it) or a highlighted ``REVIEW`` (assigned to you for review).
    REPO is the Gerrit project. SUBJECT is the commit title, placed last so it
    can run to any length. Review rows sort first within a host so "what needs
    my review" is at the top. Read-only. A per-host query failure is reported
    inline and does not abort the others.
    """
    try:
        hosts = all_hosts(user=user, password=password)
    except (VoteError, ConfigError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    total = 0
    review_total = 0
    any_error = False
    for creds in hosts:
        print(_c(f"== {creds.host} ==", "bold"))
        try:
            items = list_my_changes(creds)
        except (VoteError, ConfigError) as e:
            print(_c(f"  error: {e}", "red"))
            any_error = True
            continue
        if not items:
            print(_c("  (no non-merged changes)", "dim"))
            continue

        # Review requests first (what needs your attention), then your own.
        items = sorted(items, key=lambda i: i.is_owner)
        total += len(items)
        review_total += sum(1 for i in items if not i.is_owner)

        # Fixed-width leading columns; URL / REPO / BRANCH are padded to the
        # widest in this host's batch so SUBJECT (last, variable-length) aligns.
        url_w = max(len(i.url) for i in items)
        repo_w = max(len(i.project) for i in items)
        branch_w = max(len(i.branch) for i in items)
        print(
            _c(
                f"  {'ID':<9} {'STATUS':<9} {'REL':<6} "
                f"{'URL':<{url_w}} {'REPO':<{repo_w}} "
                f"{'BRANCH':<{branch_w}} SUBJECT",
                "bold",
            )
        )
        for i in items:
            status_color = {"NEW": "green", "ABANDONED": "yellow"}.get(i.status, "dim")
            status = _c(f"{i.status:<9}", status_color)
            if i.is_owner:
                rel = _c(f"{'owner':<6}", "dim")
            else:
                rel = _c(f"{'REVIEW':<6}", "yellow")
            repo = _c(f"{i.project:<{repo_w}}", "dim")
            branch = _c(f"{i.branch:<{branch_w}}", "dim")
            subject = i.subject or "(no subject)"
            print(
                f"  {i.number:<9} {status} {rel} "
                f"{i.url:<{url_w}} {repo} {branch} {subject}"
            )

    tally = f"{total} change(s) across {len(hosts)} host(s)"
    if review_total:
        tally += _c(f" — {review_total} need your review", "yellow")
    print("---- " + _c(tally, "bold") + " ----")
    return 1 if any_error else 0


def _version() -> str:
    """Return the package version.

    Installed metadata is the source of truth (reflects what is actually
    installed); fall back to the in-tree __init__ string for zero-install runs
    (./main.py shim, editable checkouts) where the dist metadata is absent.
    """
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("gerrit-vote")
    except PackageNotFoundError:
        from . import __version__

        return __version__


def main(argv: list[str] | None = None) -> int:
    ver = _version()
    p = argparse.ArgumentParser(
        prog="gerrit-vote",
        description=(
            f"gerrit-vote {ver} — cast a Gerrit label vote "
            "(Code-Review by default; never submits)."
        ),
    )
    p.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"gerrit-vote {ver}",
        help="Show the version and exit",
    )
    p.add_argument(
        "args",
        nargs="*",
        metavar="CHANGE... [VALUE]",
        help=(
            "One or more Gerrit change URLs (space-separated), optionally "
            "followed by a vote value (2, 1, 0, -1, -2; default 2). "
            "Omit entirely with --from-stdin/--extract."
        ),
    )
    p.add_argument(
        "--label",
        default="Code-Review",
        help=(
            "Label to vote on (default: Code-Review). Common labels: "
            "Code-Review, Verified. The set of labels — and which you may "
            "actually vote — is defined per Gerrit server and per change; run "
            "--list-labels <change> to see what is permitted on a given change."
        ),
    )
    p.add_argument(
        "--list-labels",
        metavar="CHANGE",
        help=(
            "Print the labels you may vote on CHANGE (name + allowed values, "
            "from the server's permitted_labels) and exit. Does not vote."
        ),
    )
    p.add_argument(
        "-m",
        "--message",
        help="Optional review message posted with the vote",
    )
    p.add_argument(
        "-u",
        "--user",
        help=(
            "Gerrit username override (else GERRIT_USER / config file). "
            "Applies to every host in the batch."
        ),
    )
    p.add_argument(
        "-p",
        "--password",
        help=(
            "Gerrit HTTP password override (else GERRIT_HTTP_PASSWORD / config). "
            "Note: visible in shell history; prefer env/config when possible."
        ),
    )
    p.add_argument(
        "--topic",
        metavar="TOPIC",
        help=(
            "Vote on every open change under this Gerrit topic "
            "(host = default/single configured host). A /q/topic:NAME URL "
            "works as a positional argument too."
        ),
    )
    p.add_argument(
        "--related",
        action="store_true",
        help=(
            "Also vote on each change's dependency (relation) chain, "
            "de-duplicated. Expands after topics."
        ),
    )
    p.add_argument(
        "--related-topics",
        action="store_true",
        help=(
            "Also vote on every open change sharing a topic with any change in "
            "the set. Reads each change's topic field and expands it (applied "
            "after --related, one level deep — no recursion). Auto-enabled "
            "when the source is the clipboard."
        ),
    )
    p.add_argument(
        "-c",
        "--clipboard",
        action="store_true",
        help="Extract change links from the system clipboard and vote on each",
    )
    p.add_argument(
        "--from-stdin",
        action="store_true",
        help="Extract change links from stdin and vote on each",
    )
    p.add_argument(
        "--extract",
        action="store_true",
        help="Only print change links found in stdin; do not vote",
    )
    p.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt and vote immediately",
    )
    p.add_argument(
        "--no-fallback",
        action="store_true",
        help="Disable auto-downgrade to +1 when +2 is not permitted",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve creds + read change(s) but do not vote",
    )
    p.add_argument(
        "--abandon",
        action="store_true",
        help=(
            "Abandon the change(s) instead of voting (reversible; a MERGED "
            "change is skipped). Confirmed with an arrow-key menu unless -y."
        ),
    )
    p.add_argument(
        "--restore",
        action="store_true",
        help=(
            "Restore abandoned change(s) — the inverse of --abandon. Only an "
            "ABANDONED change is restored; others are skipped. Confirmed with "
            "an arrow-key menu unless -y."
        ),
    )
    p.add_argument(
        "--delete",
        action="store_true",
        help=(
            "Delete the change(s) instead of voting. IRREVERSIBLE and "
            "owner-only (changes you don't own are skipped). Lists the changes, "
            "then confirms with an arrow-key menu (defaulting to No) before "
            "deleting. With --all, an extra menu first asks whether to sweep "
            "your open, abandoned, or both kinds of changes."
        ),
    )
    p.add_argument(
        "--force-delete",
        action="store_true",
        help=(
            "Skip the interactive confirmation for --delete (delete right "
            "away). For non-interactive/scripted use; -y does the same."
        ),
    )
    p.add_argument(
        "--all",
        action="store_true",
        help=(
            "Target ALL your changes across every configured host "
            "(owner:self; open for --abandon, abandoned for --restore). "
            "With --delete, an arrow-key menu first asks whether to sweep "
            "your open, abandoned, or both kinds of changes. Must be given "
            "explicitly and combined with --abandon, --restore, or --delete. "
            "The usual arrow-key confirmation still applies."
        ),
    )
    p.add_argument(
        "--list",
        action="store_true",
        help=(
            "List your non-merged changes across every configured host — both "
            "the ones you own (open + abandoned) and the ones assigned to you "
            "for review. Read-only: prints a table and exits, votes nothing."
        ),
    )
    args = p.parse_args(argv)
    fallback = not args.no_fallback

    # --abandon / --restore / --delete are mutually exclusive with each other.
    if sum(bool(x) for x in (args.abandon, args.restore, args.delete)) > 1:
        print(
            "error: --abandon, --restore and --delete cannot be combined.",
            file=sys.stderr,
        )
        return 2

    # --all is only meaningful with a lifecycle action. Sweeping every change of
    # yours just to VOTE on them is not a use case (and casting +2 on your whole
    # backlog would be surprising), so require one of the lifecycle actions.
    if args.all and not (args.abandon or args.restore or args.delete):
        print(
            "error: --all requires --abandon, --restore or --delete "
            "(it is a bulk-cleanup switch, not a voting one).",
            file=sys.stderr,
        )
        return 2

    # --list-labels: report what the user may vote on one change, then exit.
    # This is the authoritative per-change/per-host answer, so it is queried
    # live rather than guessed from a static list.
    if args.list_labels:
        try:
            labels = permitted_labels(
                args.list_labels, user=args.user, password=args.password
            )
        except (VoteError, ConfigError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        if not labels:
            print("No labels permitted on this change (for the current user).")
            return 0
        print("Labels you may vote on this change:")
        for name, values in labels.items():
            print(f"  {_c(name, 'bold')}: {', '.join(values)}")
        return 0

    # --list: read-only listing of your non-merged changes (owned + review
    # requests) across every configured host, then exit. Votes nothing.
    if args.list:
        return _list_my_changes(user=args.user, password=args.password)

    # Split positional args into changes + an optional trailing vote value.
    changes = list(args.args)
    value = 2
    if changes and _is_value(changes[-1]):
        value = int(changes.pop())

    # --all: sweep owner:self changes across every configured host, then jump
    # straight to the action dispatch. Bypasses clipboard/stdin/args and
    # topic/related expansion — the swept set IS the target set. The queried
    # status matches the action: restore wants abandoned changes, abandon wants
    # open ones, and delete asks (open / abandoned / both) via _pick_delete_scope.
    if args.all:
        # The sweep status is fixed by the action for abandon/restore (abandon
        # only makes sense on open changes, restore only on abandoned ones).
        # Delete is the one action where both statuses are meaningful, so offer
        # an interactive choice; -y / non-TTY fall back to "open" (the pre-choice
        # behavior) so scripts are unaffected. --dry-run still shows the menu so
        # a preview can target abandoned/both too — the scope picker is not a
        # destructive gate, only the later Yes/No confirm is.
        if args.restore:
            statuses = ["abandoned"]
        elif args.abandon:
            statuses = ["open"]
        elif not args.yes and sys.stdin.isatty():
            picked = _pick_delete_scope()
            if picked is _QUIT:
                print("Aborted; nothing deleted.")
                return 1
            statuses = ["open", "abandoned"] if picked == "all" else [picked]
        else:
            statuses = ["open"]
        phrase = "open or abandoned" if len(statuses) > 1 else statuses[0]
        try:
            hosts = all_hosts(user=args.user, password=args.password)
            urls = []
            for creds in hosts:
                for st in statuses:
                    urls.extend(my_changes(creds, status=st))
            urls = dedup_changes(urls)  # dedup across statuses
        except (VoteError, ConfigError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        if not urls:
            print(f"no {phrase} changes found for you on any configured host.")
            return 0
        print(
            _c(
                f"--all: found {len(urls)} {phrase} change(s) across "
                f"{len(hosts)} host(s).",
                "bold",
            )
        )
        if args.abandon or args.restore:
            action = abandon if args.abandon else restore
            verb = "abandon" if args.abandon else "restore"
            if not args.dry_run and not args.yes:
                if not _confirm_action(
                    urls, verb, user=args.user, password=args.password
                ):
                    print(f"Aborted; no {verb} performed.")
                    return 1
            return _lifecycle_many(
                urls,
                action,
                args.message,
                args.dry_run,
                user=args.user,
                password=args.password,
            )
        # else: --delete (guaranteed by the guard above).
        return _delete_flow(
            urls,
            dry_run=args.dry_run,
            force=args.force_delete or args.yes,
            user=args.user,
            password=args.password,
        )

    # Resolve where the change links come from:
    #   - explicit URLs on the command line  -> use them
    #   - --from-stdin                        -> read piped text
    #   - otherwise (incl. bare invocation)   -> read the system clipboard
    # This makes a bare `gerrit-vote` default to the clipboard, while any
    # positional URL bypasses the clipboard entirely.
    from_stdin = args.from_stdin
    use_clipboard = args.clipboard or (
        not changes and not from_stdin and not args.topic
    )

    topics: list[tuple[str | None, str]] = []
    if use_clipboard or from_stdin:
        source = "stdin" if from_stdin else "clipboard"
        try:
            text = sys.stdin.read() if from_stdin else _read_clipboard()
        except VoteError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        urls = extract_changes(text)
        # extract_topics yields (str, str); extend (covariant) rather than assign
        # to fit the (str | None, str) topics list — a bare list assignment trips
        # mypy's list invariance. The None host comes only from the --topic flag.
        topics.extend(extract_topics(text))
    else:
        source = "arguments"
        urls = []
        for a in changes:
            spec = parse_topic(a)
            if spec is not None:
                topics.append(spec)
            else:
                urls.append(a)

    # The --topic flag adds a host-less topic spec (resolves to default host).
    if args.topic:
        topics.append((None, args.topic))

    had_direct = bool(urls)  # any non-topic change link was supplied

    # Expand topics into change URLs (server-side filtered to open) before the
    # confirm gate / --extract, so the listed set is exactly what gets voted.
    # Dedup preserves first-seen order: direct change URLs first, then
    # topic-derived ones.
    if topics:
        try:
            expanded: list[str] = []
            for host, topic in topics:
                creds = resolve(host, user=args.user, password=args.password)
                expanded.extend(changes_in_topic(creds, topic))
        except (VoteError, ConfigError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        urls = dedup_changes(urls + expanded)

    # Clipboard is the casual "paste a patch & vote" path: auto-expand the whole
    # related group (dependency chain + topic siblings) so a bare `gerrit-vote`
    # votes on the full set, not just the pasted patch. Explicit URL args are the
    # precise path — no auto-expand (escape hatch: pass the URL explicitly).
    # Destructive actions never auto-expand: you abandon/delete exactly what you
    # point at. --extract is excluded too: it is a pure "parse my clipboard"
    # preview, and --dry-run already shows the expanded set with full detail.
    # --from-stdin is excluded (scripted callers want determinism).
    if (
        use_clipboard
        and not (args.abandon or args.restore or args.delete)
        and not args.extract
    ):
        args.related = True
        args.related_topics = True

    # Resolve /q/I<Change-Id> URLs (clipboard/stdin/args) to /c/<number>: such
    # URLs carry no change number, so parse_change()/vote() cannot consume them
    # and --related expansion (which calls parse_change) would crash on them.
    # Done before --related and before every action dispatch so all paths see
    # plain /c/N URLs. --extract is a pure offline parse preview — skip it so
    # the preview stays network-free and prints exactly what was extracted.
    if not args.extract and urls:
        try:
            urls, cid_warnings = resolve_change_ids(
                urls, user=args.user, password=args.password
            )
        except (VoteError, ConfigError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        for w in cid_warnings:
            print(_c(f"warning: {w}", "yellow"), file=sys.stderr)

    # --related: expand each change to its dependency chain. A relation chain is
    # host-local, so each change resolves its own host's credentials (the batch
    # may span hosts). Dedup preserves first-seen order.
    if args.related and urls:
        try:
            related: list[str] = []
            for u in urls:
                related.append(u)
                host, _ = parse_change(u)
                creds = resolve(host, user=args.user, password=args.password)
                related.extend(related_changes(creds, u))
        except (VoteError, ConfigError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        urls = dedup_changes(related)

    # --related-topics: for every change in the set, read its topic; pull in all
    # open changes under each distinct (host, topic). Grouped by host because a
    # topic query is host-local (topic "foo" on host A != host B). One level
    # only: topic-pulled changes are NOT re-chain-expanded. Dedup preserves
    # first-seen order (direct URLs, then chain, then topic siblings).
    if args.related_topics and urls:
        try:
            by_host: dict[str | None, list[str]] = {}
            for u in urls:
                host, _ = parse_change(u)
                by_host.setdefault(host, []).append(u)
            sibling: dict[str, None] = {}
            for host, group in by_host.items():
                creds = resolve(host, user=args.user, password=args.password)
                topics_seen: set[str] = set()
                for u in group:
                    t = change_topic(creds, u)
                    if t:
                        topics_seen.add(t)
                for t in topics_seen:
                    for sib in changes_in_topic(creds, t):
                        sibling.setdefault(sib, None)
            urls = dedup_changes(urls + list(sibling))
        except (VoteError, ConfigError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 1

    # When only topics were given and none had open changes, say so plainly
    # rather than blaming the source for missing links.
    empty_msg = (
        "no open changes found for the given topic(s)"
        if topics and not had_direct
        else f"no gerrit change links found in {source}"
    )

    if args.extract:
        for u in urls:
            print(u)
        if not urls:
            print(empty_msg, file=sys.stderr)
            return 1
        return 0

    if not urls:
        print(empty_msg, file=sys.stderr)
        return 1

    # --abandon / --restore: reversible lifecycle actions. Arrow-key confirm
    # unless -y or --dry-run. Both go through _lifecycle_many (abandon/restore
    # share a signature); the action fn decides the endpoint + eligible status.
    if args.abandon or args.restore:
        action = abandon if args.abandon else restore
        verb = "abandon" if args.abandon else "restore"
        if not args.dry_run and not args.yes:
            if not _confirm_action(urls, verb, user=args.user, password=args.password):
                print(f"Aborted; no {verb} performed.")
                return 1
        return _lifecycle_many(
            urls,
            action,
            args.message,
            args.dry_run,
            user=args.user,
            password=args.password,
        )

    # --delete: irreversible + owner-only. List -> arrow-key confirm -> delete,
    # via the shared _delete_flow (same path --all --delete uses). --force-delete
    # or -y skips the confirm; --dry-run prints a preview and stops.
    if args.delete:
        return _delete_flow(
            urls,
            dry_run=args.dry_run,
            force=args.force_delete or args.yes,
            user=args.user,
            password=args.password,
        )

    # Confirmation gate: list the changes and let the user pick the value via
    # an arrow-key menu (+2 default / +1 / quit). Skipped for --dry-run (casts
    # nothing) and when -y/--yes is given (uses the value from the command line).
    if not args.dry_run and not args.yes:
        picked = _confirm_and_pick_value(
            urls, args.label, user=args.user, password=args.password
        )
        if picked is _QUIT:
            print("Aborted; no votes cast.")
            return 1
        value = picked

    return _vote_many(
        urls,
        value,
        args.label,
        args.message,
        args.dry_run,
        fallback,
        user=args.user,
        password=args.password,
    )


if __name__ == "__main__":
    raise SystemExit(main())
