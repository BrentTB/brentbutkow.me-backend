from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.modules.recalls.analytics import rebuild_analytics
from app.modules.recalls.models import Recall, RecallAnalyticsBuild, RecallTopic

NAME = "analytics"


def status(session: Session) -> tuple[bool, str]:
    # Themes + neighbours are a whole-corpus rebuild that doesn't self-populate at ingest. A row
    # with no topic can be legitimate for recalls that don't have enough usable text to cluster,
    # so use an explicit build marker instead of the nullable topic column.
    topics = session.scalar(select(func.count()).select_from(RecallTopic)) or 0

    built_at = session.scalar(select(func.max(RecallAnalyticsBuild.built_at)))
    if built_at is None:
        return True, "themes + neighbours not built yet"

    last_change = session.scalar(select(func.max(Recall.updated_at)))
    if last_change is not None and last_change > built_at:
        return True, "recalls changed since the last analytics build"

    return False, f"themes + neighbours built ({topics} topics)"


# Rebuilds the derived analytics over every stored recall, from one shared TF-IDF matrix: NMF themes
# (recall_topics + recalls.topic_id) and cosine nearest-neighbour similarity (recall_neighbors). Run
# after ingest, or after changing the analytics params: `python -m scripts.build_analytics`.
def main() -> None:
    session = SessionLocal()
    try:
        summary = rebuild_analytics(session)
        print(
            f"Rebuilt analytics: {summary['recalls']} recalls, "
            f"{summary['topics']} topics, {summary['neighbors']} neighbour links."
        )
    finally:
        session.close()


if __name__ == "__main__":
    main()
