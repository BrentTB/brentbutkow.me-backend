from collections.abc import Callable

from app.db import SessionLocal
from app.modules.recalls.analytics import rebuild_analytics
from app.modules.recalls.schemas import IngestResult
from app.modules.recalls.service import run_fda_ingest, run_fsis_ingest, run_uk_ingest

# Every source the daily ingest covers, in run order — mirrors .github/workflows/ingest.yml.
_INGESTS: tuple[tuple[str, Callable[..., IngestResult]], ...] = (
    ("FDA", run_fda_ingest),
    ("FSIS", run_fsis_ingest),
    ("UK FSA", run_uk_ingest),
)


# Runs every source ingest in one pass. Each source is isolated: if one fails (a flaky upstream,
# say) the others still run, and the script exits non-zero if any failed so a caller/CI notices.
def main() -> None:
    session = SessionLocal()
    failures: list[str] = []
    try:
        for label, run in _INGESTS:
            try:
                result = run(session)
                print(
                    f"{label}: fetched {result.fetched}, "
                    f"{result.new} new, upserted {result.upserted}."
                )
            except Exception as exc:
                failures.append(label)
                print(f"{label}: FAILED — {exc}")
    finally:
        session.close()

    # Themes + similar-recall neighbours are a whole-corpus rebuild, so refresh them once here after
    # the per-source ingests — not per row. Isolated like a source: a failure is flagged but leaves
    # the freshly-ingested recalls intact.
    try:
        analytics_session = SessionLocal()
        try:
            summary = rebuild_analytics(analytics_session)
            print(
                f"Analytics: {summary['topics']} topics, "
                f"{summary['neighbors']} neighbour links over {summary['recalls']} recalls."
            )
        finally:
            analytics_session.close()
    except Exception as exc:
        failures.append("analytics")
        print(f"Analytics: FAILED — {exc}")

    if failures:
        raise SystemExit(
            f"Pipeline finished with {len(failures)} failure(s): {', '.join(failures)}."
        )
    print("All ingests complete, analytics rebuilt.")


if __name__ == "__main__":
    main()
