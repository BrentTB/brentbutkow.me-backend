from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.modules.recalls.class_predictor import PREDICT_COUNTRIES, rebuild_predictions
from app.modules.recalls.models import Recall

NAME = "predictions"


def status(session: Session) -> tuple[bool, str]:
    # The predicted class doesn't self-populate at ingest, so a UK/ZA recall with usable text and no
    # prediction means the build hasn't run for it (or ran before the recall was ingested).
    missing = (
        session.scalar(
            select(func.count())
            .select_from(Recall)
            .where(Recall.country.in_(PREDICT_COUNTRIES))
            .where(Recall.predicted_class.is_(None))
            .where(func.length(func.trim(Recall.reason_text)) > 0)
        )
        or 0
    )
    if missing:
        return True, f"{missing} UK/ZA recalls without a predicted class"
    return False, "predicted classes built"


# Predicts + materialises recalls.predicted_class / predicted_class_confidence for the countries
# with no native class system (UK, ZA), using the committed class_predictor.joblib. Run after
# ingest, or after retraining the model: `python -m scripts.build_predictions`.
def main() -> None:
    session = SessionLocal()
    try:
        summary = rebuild_predictions(session)
        print(
            f"Rebuilt predictions: {summary['predicted']} of {summary['recalls']} "
            "UK/ZA recalls given a predicted class."
        )
    finally:
        session.close()


if __name__ == "__main__":
    main()
