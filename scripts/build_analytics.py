from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.modules.recalls.analytics import rebuild_analytics
from app.modules.recalls.models import Recall, RecallTopic

NAME = "analytics"


def status(session: Session) -> tuple[bool, str]:
    # Unlike the per-row backfills, themes + neighbours are a whole-corpus rebuild and don't
    # self-populate at ingest — so any text-bearing recall without a topic means the build is stale.
    topics = session.scalar(select(func.count()).select_from(RecallTopic)) or 0
    if topics == 0:
        return True, "themes + neighbours not built yet"
    missing = (
        session.scalar(
            select(func.count())
            .select_from(Recall)
            .where(
                Recall.topic_id.is_(None),
                or_(
                    func.coalesce(Recall.reason_text, "") != "",
                    func.coalesce(Recall.product_description, "") != "",
                ),
            )
        )
        or 0
    )
    if missing:
        return True, f"{missing} recall(s) added since the last build"
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
