import argparse
from typing import Protocol

from sqlalchemy.orm import Session

from app.db import SessionLocal
from scripts import backfill, backfill_entities, backfill_severity


class _Backfill(Protocol):
    # The contract every backfill module satisfies: a display name, a status check that reports
    # whether it still has work (and why), and a main() that runs it. Modules match structurally.
    NAME: str

    def status(self, session: Session) -> tuple[bool, str]: ...

    def main(self) -> None: ...


# Each backfill module owns its own "do I still need to run?" logic (its `status`), so adding a new
# backfill is just dropping in a scripts/backfill_*.py with NAME + status() + main() and adding it to
# this list — no changes to the others.
_BACKFILLS: list[_Backfill] = [backfill, backfill_severity, backfill_entities]


# Runs the data backfills, skipping any whose own status reports it's already done. `--all` forces
# every one; `--check` prints the plan and exits without running anything.
def main() -> None:
    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument(
        "--all", action="store_true", help="run every backfill regardless of its status"
    )
    parser.add_argument(
        "--check", action="store_true", help="print the plan and exit without running anything"
    )
    args = parser.parse_args()

    session = SessionLocal()
    try:
        plan = [(bf, *bf.status(session)) for bf in _BACKFILLS]
    finally:
        session.close()

    for bf, needed, reason in plan:
        print(f"[{'RUN ' if args.all or needed else 'skip'}] {bf.NAME}: {reason}")

    if args.check:
        return

    ran = 0
    for bf, needed, reason in plan:
        if args.all or needed:
            print(f"\n=== {bf.NAME} ===")
            bf.main()
            ran += 1

    print(f"\nBackfill-all complete ({ran} of {len(plan)} run).")


if __name__ == "__main__":
    main()
