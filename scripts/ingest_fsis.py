from app.db import SessionLocal
from app.modules.recalls.service import run_fsis_ingest


def main() -> None:
    session = SessionLocal()
    try:
        result = run_fsis_ingest(session)
        print(f"FSIS ingest complete: fetched {result.fetched}, upserted {result.upserted}.")
    finally:
        session.close()


if __name__ == "__main__":
    main()
