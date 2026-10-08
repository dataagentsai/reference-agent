"""Re-export stub: moved to `agent_harness.contracts.tools` (T-019).

The old name is the same module object, so every import, private name and patch
made through it reaches the library's code.
"""

import sys

from agent_harness.contracts import tools as _moved

sys.modules[__name__] = _moved
