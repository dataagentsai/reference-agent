"""Re-export stub: moved to `agent_harness.telemetry.meters` (T-019).

The old name is the same module object, so every import, private name and patch
made through it reaches the library's code.
"""

import sys

from agent_harness.telemetry import meters as _moved

sys.modules[__name__] = _moved
