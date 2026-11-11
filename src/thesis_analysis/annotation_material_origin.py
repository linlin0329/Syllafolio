'Identify material by registered test-package hashes; never infer species from filenames.'
from functools import lru_cache
import json
from pathlib import Path


@lru_cache(maxsize=1)
def registry():
    path = Path(__file__).resolve().parents[2] / "configs/annotation_manual/test_material_hashes.json"
    return json.loads(path.read_text())


def classify_material(hashes):
    hashes = set(hashes)
    known = registry()
    synthetic, real = set(known["synthetic_sha256"]), set(known["real_sparrow_sha256"])
    if hashes and hashes <= synthetic:
        return {"kind": "synthetic", "label": 'Synthetic test audio · not real birdsong',
                "detail": 'File hashes match the registered synthetic test package; use only to practice operation and computation, not for tree sparrow acoustic judgments.'}
    if hashes and hashes <= real:
        return {"kind": "real_sparrow", "label": 'Real tree sparrow recordings · engineering test material',
                "detail": 'File hashes match the reviewed real-recording test package; type judgments still require actual human listening.'}
    if hashes & synthetic:
        return {"kind": "mixed", "label": 'Mixed material · includes known synthetic test audio',
                "detail": 'Some files match the synthetic test package; other origins have not been verified by this application. Check each material separately.'}
    return {"kind": "unverified", "label": 'User-imported audio · material not verified',
            "detail": 'These files were not identified as a registered test package; the material provider must confirm real or synthetic origin and species.'}
