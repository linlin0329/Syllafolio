"""Portable launcher with isolated current-run logs."""
from pathlib import Path
import subprocess
import sys
import traceback
sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parent


def main():
    from runtime_paths import configure_runtime
    from prototype_reports import atomic_json, begin_run, new_run_id, publish_launch
    config = configure_runtime(ROOT, run_id=new_run_id("launch"))
    folder = begin_run(ROOT, "launch", config["run_id"], config)
    publish_launch(ROOT, config["run_id"], {"run_id": config["run_id"], "diagnostics_dir": str(folder), "status": "starting"})
    print("Starting Syllafolio with no example project. First import of libraries may take a moment.", flush=True)
    print("Launch log: " + str(folder / "startup.log"), flush=True)
    code = 1
    try:
        with (folder / "startup.log").open("w", encoding="utf-8") as log:
            process = subprocess.Popen([sys.executable, "-B", "-X", "utf8", str(ROOT / "launch.py"), *sys.argv[1:]],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
            for line in process.stdout:
                print(line, end="", flush=True); log.write(line); log.flush()
            code = process.wait()
    except Exception:
        detail = traceback.format_exc(); (folder / "startup.log").write_text(detail, encoding="utf-8")
        print(detail, file=sys.stderr)
    result = {"run_id": config["run_id"], "exit_code": code, "status": "process_exited" if code == 0 else "launch_failed",
              "native_windows_process": sys.platform == "win32", "version": "0.1.0",
              "diagnostics_dir": str(folder), "windows_acceptance_complete": False}
    atomic_json(folder / "launch_result.json", result); publish_launch(ROOT, config["run_id"], result)
    return code


if __name__ == "__main__":
    from installation_lease import InstallationLease
    with InstallationLease(ROOT, mode="shared"):
        raise SystemExit(main())
