from sqlalchemy import select
from sqlalchemy.orm import Session, load_only

from app.db import SessionLocal
from app.modules.recalls.analytics import _compose_text
from app.modules.recalls.class_predictor import PREDICT_COUNTRIES, rebuild_predictions
from app.modules.recalls.models import Recall

NAME = "predictions"


def status(session: Session) -> tuple[bool, str]:
    # The predicted class doesn't self-populate at ingest, so a UK/ZA recall with usable text and no
    # prediction means the build hasn't run for it (or ran before the recall was ingested).
    # "Usable" must match rebuild_predictions exactly: it predicts only rows whose composed text
    # (reason + product, firm name stripped) is non-empty. Checking reason_text alone would flag a
    # company-name-only recall as perpetually "missing" — rebuild_predictions permanently skips it,
    # so it would never satisfy the gate.
    candidates = session.scalars(
        select(Recall)
        .options(load_only(Recall.reason_text, Recall.product_description, Recall.company_name))
        .where(Recall.country.in_(PREDICT_COUNTRIES))
        .where(Recall.predicted_class.is_(None))
    ).all()
    missing = sum(
        1 for r in candidates if _compose_text(r.reason_text, r.product_description, r.company_name)
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
