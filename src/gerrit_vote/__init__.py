# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Junbo Zheng. All rights reserved.
"""gerrit-vote: cast label votes and manage Gerrit changes from the CLI.

Votes (Code-Review by default) never submit or merge; opt-in lifecycle actions
(abandon / restore / delete) and a read-only listing round out the toolset.
"""

from .config import ConfigError, Credentials, all_hosts, resolve
from .core import (
    ChangeListItem,
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

__version__ = "0.0.6"

__all__ = [
    "ChangeListItem",
    "ChangeMeta",
    "ConfigError",
    "Credentials",
    "VoteError",
    "VoteResult",
    "abandon",
    "all_hosts",
    "change_meta",
    "change_topic",
    "changes_in_topic",
    "dedup_changes",
    "delete_change",
    "extract_changes",
    "extract_topics",
    "git_identity_email",
    "list_my_changes",
    "my_changes",
    "parse_change",
    "parse_topic",
    "permitted_labels",
    "related_changes",
    "resolve",
    "resolve_change_ids",
    "restore",
    "vote",
    "vote_labels",
    "__version__",
]
