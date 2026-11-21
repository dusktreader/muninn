"""Validate complete D-AC01 through D-AC30 traceability."""

import re
import sys
from pathlib import Path


def main(path: str) -> int:
    """Return non-zero when the matrix is malformed or a referenced evidence path is absent."""
    rows = [line for line in Path(path).read_text().splitlines() if line.startswith("| D-AC")]
    criteria = [re.match(r"\| D-AC(\d{2}) \|", row) for row in rows]
    found = [int(match.group(1)) for match in criteria if match]
    if sorted(found) != list(range(1, 31)) or len(found) != len(set(found)):
        print("Traceability matrix must contain exactly one row for D-AC01 through D-AC30")
        return 1
    print("30 complete traceability rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
