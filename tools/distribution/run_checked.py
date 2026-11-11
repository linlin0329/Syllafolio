"""Compatibility entry for the data-free Lite package self-check."""
from pathlib import Path
import sys
sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parent


def main():
    if len(sys.argv) < 2 or sys.argv[1] != "check":
        print("Lite supports: run_checked.py check [--static-only]. No acceptance datasets or long-material suite are included.")
        return 2
    sys.argv = [str(ROOT / "check_prototype.py"), *sys.argv[2:]]
    from check_prototype import main as check
    return check()


if __name__ == "__main__":
    from installation_lease import InstallationLease
    with InstallationLease(ROOT, mode="shared"):
        raise SystemExit(main())
