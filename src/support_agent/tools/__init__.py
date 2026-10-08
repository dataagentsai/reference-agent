"""Re-export stub: moved to `agent_harness.tools` (T-019).

A package keeps its own module so its submodules' stubs still resolve here;
every attribute read or written through it is the library package's, except a
submodule bound by an import, which stays on this package.
"""

import sys
from types import ModuleType
from typing import Any

import agent_harness.tools as _moved


class _Forward(ModuleType):
    def __getattr__(self, name: str) -> Any:
        return getattr(_moved, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if isinstance(value, ModuleType):
            super().__setattr__(name, value)
        else:
            setattr(_moved, name, value)


sys.modules[__name__].__class__ = _Forward
