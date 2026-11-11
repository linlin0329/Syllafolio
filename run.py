"""Start the desktop application from a source checkout."""
from pathlib import Path
import sys


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
    from thesis_analysis.annotation_desktop import main

    raise SystemExit(main())
