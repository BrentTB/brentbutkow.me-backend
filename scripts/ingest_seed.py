from app.db import SessionLocal
from app.modules.recalls.service import run_seed_ingest


def main() -> None:
    session = SessionLocal()
    try:
        result = run_seed_ingest(session)
        print(
            f"SA seed ingest complete: fetched {result.fetched}, "
            f"{result.new} new, upserted {result.upserted}."
        )
    finally:
        session.close()


if __name__ == "__main__":
    main()
