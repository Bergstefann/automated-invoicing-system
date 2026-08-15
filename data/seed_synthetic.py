"""Standalone entry point for generating the synthetic demo dataset.

    python data/seed_synthetic.py [path-to-db]

The actual generator lives in `invoicing.seed` (part of the installed
package) so `invoicing run --demo` can reuse it directly; this script just
gives you a way to produce a demo database file without going through the
CLI, e.g. for inspection.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from invoicing.db import Database  # noqa: E402
from invoicing.seed import seed_database  # noqa: E402


def main() -> None:
    db_path = sys.argv[1] if len(sys.argv) > 1 else "demo.db"
    with Database(db_path) as db:
        seed_database(db)
    print(f"Seeded synthetic dataset into {db_path}")


if __name__ == "__main__":
    main()
