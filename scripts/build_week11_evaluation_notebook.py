"""Build the standalone feedback-led review from saved data; no new simulation."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.build_week11_notebook import execute_notebook
from scripts.week11_revision_notebook import evaluation_cells


def build():
    execute_notebook(evaluation_cells(), ROOT / "notebooks/week11_evaluation_review.ipynb")


if __name__ == "__main__":
    build()
