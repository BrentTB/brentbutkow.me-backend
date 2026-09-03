import subprocess
import sys
import textwrap
from pathlib import Path

from app.modules.recalls.classifier import classify
from app.modules.recalls.schemas import RecallCategory

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_names_a_cause_with_full_confidence():
    assert classify("Product contains undeclared milk.") == (RecallCategory.allergen, 1.0)
    assert classify("Potential Listeria monocytogenes contamination.") == (
        RecallCategory.pathogen,
        1.0,
    )


def test_unnamed_cause_falls_through_to_other_with_zero_confidence():
    assert classify("Quality defect of unknown origin.") == (RecallCategory.other, 0.0)


def test_gazetteer_decides_over_an_incidental_ingredient_word():
    # "raw milk cheese recalled for E. coli" — the named pathogen is the cause, not the ingredient.
    category, confidence = classify("Raw milk cheese recalled due to E. coli O157:H7")
    assert category == RecallCategory.pathogen
    assert confidence == 1.0


def test_confidence_is_always_in_range():
    for text in ("Undeclared peanuts", "Metal fragments found", "", "Unspecified issue"):
        _, confidence = classify(text)
        assert 0.0 <= confidence <= 1.0


# The API process runs on a 512 MB box. joblib.load() of the old classifier pulled in the
# sklearn/scipy import graph — ~116 MB, and a contributor to the 2026-09-02 OOM. CLAUDE.md's rule
# is that the ML stack loads only in offline scripts, never on the request path; this is the guard.
# It runs in a subprocess because the offline-script tests in this suite import sklearn themselves,
# so an in-process sys.modules check would pass or fail depending on test ordering.
def test_ingest_path_never_imports_the_ml_stack():
    probe = textwrap.dedent(
        """
        import sys

        # The ingest request path: the service module imports every source normalizer, and each one
        # classifies as it normalizes.
        from app.modules.recalls.service import run_cfia_ingest  # noqa: F401
        from app.modules.recalls.classifier import classify

        classify("Undeclared milk in chocolate bars.")

        heavy = sorted(m for m in ("sklearn", "scipy", "joblib") if m in sys.modules)
        print(",".join(heavy))
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "", (
        f"the ingest path imported the ML stack: {result.stdout.strip()}"
    )
