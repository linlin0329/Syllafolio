"""Application-local runtime paths; no global Python or registry installation."""
from pathlib import Path
import os

ROOT = Path(__file__).resolve().parent
os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("NUMBA_NUM_THREADS", "1")
# Python executes this before the entry script can acquire its lease. Keep it
# stdlib-only and read-only; each supported entry configures runtime paths only
# after acquiring its own shared lease. Updater startup must stay lock-free here.
_dll_handles = []
if os.name == "nt" and hasattr(os, "add_dll_directory"):
    for relative in ["_runtime", "site-packages/PySide6", "site-packages/shiboken6",
                     "site-packages/numpy.libs", "site-packages/scipy.libs", "site-packages/sklearn.libs"]:
        folder = ROOT / relative
        if folder.is_dir():
            _dll_handles.append(os.add_dll_directory(str(folder)))
