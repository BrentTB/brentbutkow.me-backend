from app.db import SessionLocal
from app.modules.recalls.service import run_ingest


def main() -> None:
    session = SessionLocal()
    try:
        result = run_ingest(session)
        print(f"Ingest complete: fetched {result.fetched}, upserted {result.upserted}.")
    finally:
        session.close()


if __name__ == "__main__":
    main()
