"""Entry point: run the adversarial progression monitor on a patient chart.

Usage:
    python main.py --patient corpus/patient1
    python main.py --patient corpus/patient1 --slice-through visit2   # V1-V2 only
"""
import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

from orchestrator import run


def main():
    ap = argparse.ArgumentParser(description="Psoriasis progression monitor")
    ap.add_argument("--patient", default="corpus/patient1")
    ap.add_argument(
        "--slice-through",
        default=None,
        metavar="PREFIX",
        help="only include notes whose filename sorts <= this prefix (e.g. visit2)",
    )
    args = ap.parse_args()

    patient_dir = Path(args.patient)
    if not patient_dir.is_dir():
        sys.exit(f"patient dir not found: {patient_dir}")

    if args.slice_through:
        # Build a temporary sliced copy so doc_ids stay identical.
        tmp = Path(tempfile.mkdtemp(prefix=f"{patient_dir.name}_slice_"))
        sliced = tmp / f"{patient_dir.name}_slice"
        sliced.mkdir()
        cutoff = args.slice_through
        for f in sorted(patient_dir.glob("*.md")):
            if f.stem.split("_")[0] <= cutoff:
                shutil.copy(f, sliced / f.name)
        # Carry the in-range chart photographs so the vision pass still runs.
        images = [
            f for f in sorted((patient_dir / "images").glob("*"))
            if f.suffix.lower() in (".jpg", ".jpeg", ".png")
            and f.stem.split("_")[0] <= cutoff
        ]
        if images:
            (sliced / "images").mkdir()
            for f in images:
                shutil.copy(f, sliced / "images" / f.name)
        patient_dir = sliced

    state = run(str(patient_dir))
    print(json.dumps({"patient_id": state["patient_id"],
                      "round": state["round"],
                      "terminal_state": state["terminal_state"]}, indent=2))


if __name__ == "__main__":
    main()
