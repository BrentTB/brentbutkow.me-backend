from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.modules.recalls.events import rebuild_events
from app.modules.recalls.models import Recall, RecallEvent

NAME = "events"


def status(session: Session) -> tuple[bool, str]:
    # Event clusters are a whole-corpus rebuild that runs right after build_analytics (it reuses the
    # neighbour graph) and doesn't self-populate at ingest. Most recalls are singletons with no
    # cluster, so "a recall without event_cluster_id" can't signal staleness the way topic_id does —
    # we only flag the never-built case here; ingest_all refreshes events daily alongside analytics.
    events = session.scalar(select(func.count()).select_from(RecallEvent)) or 0
    if events == 0 and (session.scalar(select(func.count()).select_from(Recall)) or 0):
        return True, "event clusters not built yet"
    return False, f"event clusters built ({events} events)"


# Rebuilds recall_events + recalls.event_cluster_id by clustering recalls into incidents from the
# similarity graph (recall_neighbors) + shared pathogens within a time window. Run AFTER
# build_analytics (it consumes the neighbours): `python -m scripts.build_events`.
def main() -> None:
    session = SessionLocal()
    try:
        summary = rebuild_events(session)
        print(
            f"Rebuilt events: {summary['recalls']} recalls, "
            f"{summary['events']} events ({summary['outbreaks']} outbreaks)."
        )
    finally:
        session.close()


if __name__ == "__main__":
    main()
