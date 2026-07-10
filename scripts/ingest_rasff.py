from app.db import SessionLocal
from app.modules.recalls.service import run_rasff_ingest


def main() -> None:
    session = SessionLocal()
    try:
        result = run_rasff_ingest(session)
        print(
            f"RASFF (EU) ingest complete: fetched {result.fetched}, "
            f"{result.new} new, upserted {result.upserted}."
        )
    finally:
        session.close()


if __name__ == "__main__":
    main()
