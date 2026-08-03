"""Shared pytest configuration for the fusion-mcp test suite.

All tests in this suite are HEADLESS: they exercise pure-logic modules
(``mcp_server/scad_translator.py``, ``mcp_server/bundle.py``) without Fusion
360 running and without any network access.
"""

import os
import sys

# Make the repo root importable so ``from mcp_server import ...`` works from
# the tests/ directory (mcp_server is a namespace package, no __init__.py).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class TrapRoot:
    """A Fusion root-component stub whose every attribute access fails.

    Passing this as ``root`` to ``translate_to_fusion_commands()`` proves the
    two-phase design: the pure validation phase must complete WITHOUT ever
    touching the Fusion API (any attribute access raises AssertionError).  A
    tree that passes validation instead reaches phase 2, which raises the
    "adsk is not available" RuntimeError before any root attribute is read.
    """

    def __getattr__(self, name):
        raise AssertionError(
            "Fusion root was accessed during the pure validation phase: %r"
            % name)
