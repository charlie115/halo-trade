"""The one door between the open part of this service and its decision engine.

The engine (the decision loop, its research tools, memory, the market desk and the chat assistant)
is kept in a separate, private tree. Everything else reaches it only through `engine()`, and runs
without it: nothing decides, so no order is placed, and a wallet is told the agent is not available.
The rules an order must pass, the signing path and the stop watcher do not depend on it.
"""
from __future__ import annotations

import importlib
from types import ModuleType
from typing import Optional

ENGINE = "halo_trade.engine"


def engine() -> Optional[ModuleType]:
    """The engine module, or None when this tree was built without it. Any other import error is raised."""
    try:
        return importlib.import_module(ENGINE)
    except ModuleNotFoundError as exc:
        if exc.name != ENGINE:
            raise
        return None
