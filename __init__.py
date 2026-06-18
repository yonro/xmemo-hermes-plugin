"""XMemo memory provider plugin shim.

This root-level shim makes the whole repository cloneable directly into
``$HERMES_HOME/plugins/xmemo/`` as documented in README.md. The actual
implementation lives in the ``xmemo/`` package below.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make the nested xmemo/ package importable when this directory is loaded by
# Hermes' external memory-provider discovery (which does not put the plugin
# directory on sys.path by default).
_provider_dir = Path(__file__).parent
if str(_provider_dir) not in sys.path:
    sys.path.insert(0, str(_provider_dir))

from xmemo import XMemoMemoryProvider


def register(ctx) -> None:
    """Register XMemo as a memory provider plugin."""
    ctx.register_memory_provider(XMemoMemoryProvider())
