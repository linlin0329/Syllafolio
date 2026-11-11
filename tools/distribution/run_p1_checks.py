"""Explain the omitted acceptance suite; do not report missing-data tests as passed."""
from pathlib import Path
import sys
sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parent


def main():
    print("This distribution does not include the development acceptance suite or datasets. That suite was not run.")
    print("Use Run_Checks.cmd for the package integrity/import/blank-startup self-check.")
    return 2


if __name__ == "__main__":
    from installation_lease import InstallationLease
    with InstallationLease(ROOT, mode="shared"):
        raise SystemExit(main())
