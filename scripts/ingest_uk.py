from app.db import SessionLocal
from app.modules.recalls.service import run_uk_ingest


def main() -> None:
    session = SessionLocal()
    try:
        result = run_uk_ingest(session)
        print(
            f"UK FSA ingest complete: fetched {result.fetched}, "
            f"{result.new} new, upserted {result.upserted}."
        )
    finally:
        session.close()


if __name__ == "__main__":
    main()
