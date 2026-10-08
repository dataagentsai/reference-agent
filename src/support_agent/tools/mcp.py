"""Re-export stub: moved to `agent_harness.tools.mcp` (T-019).

The old name is the same module object, so every import, private name and patch
made through it reaches the library's code.
"""

import importlib
import sys

sys.modules[__name__] = importlib.import_module("agent_harness.tools.mcp")
