"""Record the numbers this release is measured against.

Run deliberately, never automatically. A baseline that regenerates itself is not
a baseline — it is a record of whatever happened last, and it agrees with every
regression.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_golden import BASELINE, measured  # noqa: E402


def main() -> None:
    payload = {
        "taken": date.today().isoformat(),
        "note": "Regenerate deliberately. Say in the commit why a number moved.",
        "measurements": measured(),
    }
    BASELINE.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"baseline written to {BASELINE}")
    for k, v in payload["measurements"].items():
        print(f"  {k:22} {v}")


if __name__ == "__main__":
    main()
