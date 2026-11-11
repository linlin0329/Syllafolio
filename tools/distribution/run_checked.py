"""Compatibility entry for the data-free package self-check."""
from pathlib import Path
import sys
sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parent


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in {"self_check", "check"}:
        print("Usage: run_checked.py self_check [--static-only]. No development acceptance datasets or suite are included.")
        return 2
    sys.argv = [str(ROOT / "check_prototype.py"), *sys.argv[2:]]
    from check_prototype import main as check
    return check()


if __name__ == "__main__":
    from installation_lease import InstallationLease
    with InstallationLease(ROOT, mode="shared"):
        raise SystemExit(main())
