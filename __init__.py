"""XMemo memory provider plugin shim.

This root-level shim makes the whole repository cloneable directly into
``$HERMES_HOME/plugins/xmemo/`` as documented in README.md. The actual
implementation lives in the ``xmemo/`` package below.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Hermes loads this directory as a package, so prefer a relative import of the
# nested implementation package. This avoids collisions with any existing
# ``sys.modules["xmemo"]`` that may already be present in the process.
try:
    from .xmemo import XMemoMemoryProvider
except ImportError:
    # Fallback for environments that import this file as a bare module
    # (e.g., pytest collecting a repo whose directory name contains '-').
    _provider_dir = Path(__file__).parent
    if str(_provider_dir) not in sys.path:
        sys.path.insert(0, str(_provider_dir))
    from xmemo import XMemoMemoryProvider


def register(ctx) -> None:
    """Register XMemo as a memory provider plugin."""
    ctx.register_memory_provider(XMemoMemoryProvider())
