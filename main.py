#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Junbo Zheng. All rights reserved.
"""Run gerrit-vote straight from the source tree, no install needed.

    ./main.py <args>

Prepends src/ to sys.path so it imports the working-tree package, exactly like
the installed console script but without a build/install step.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from gerrit_vote.cli import main  # noqa: E402

raise SystemExit(main())
