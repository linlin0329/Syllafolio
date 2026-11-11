"""Explain the omitted acceptance suite; do not report missing-data tests as passed."""
from pathlib import Path
import sys
sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parent


def main():
    print("The Lite package does not include the application acceptance suite or datasets. No P1 tests were run.")
    print("Use Run_Checks.cmd for the Lite integrity/import/blank-startup self-check.")
    return 2


if __name__ == "__main__":
    from installation_lease import InstallationLease
    with InstallationLease(ROOT, mode="shared"):
        raise SystemExit(main())
