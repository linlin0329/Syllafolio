"""A member lock, an early open round and the final completion gate have different meanings."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from syllafolio.annotation_desktop_pool_review import pool_guidance


class PoolGuidanceTests(unittest.TestCase):
    def test_changed_early_pool_keeps_existing_pages_and_names_the_available_action(self):
        pool = {"organization": {"groups": ["A"]}, "pool": {"active_ids": ["a", "b"], "can_prepare_current_pool": True}}
        self.assertIn("Prepare group", pool_guidance(pool, {}))
        r = {"status": "ready", "pool_mode": "current_pool", "item_locks": {"a": {"code": "BATCH_ITEM_OVERLAP"}}}
        self.assertIn("save unlocked", pool_guidance(pool, r))
        self.assertIn("locked syllable", pool_guidance(pool, r))
        changed = dict(r, pool_refresh_required=True, new_pool_item_ids=["b"])
        self.assertIn("1 new pool member", pool_guidance(pool, changed))
        self.assertIn("Prepare again", pool_guidance(pool, changed))
        self.assertIn("Prepare group", pool_guidance(pool, dict(changed, closed=True)))

    def test_empty_and_closed_pool_do_not_imply_end_of_first_pass(self):
        batch = {"organization": {"groups": ["A"]}, "pool": {"active_ids": []}}
        self.assertIn("Finish outstanding", pool_guidance(batch, {}))
        done = {"pool_mode": "current_pool", "closed": True, "status": "ready"}
        self.assertIn("remaining candidate groups", pool_guidance(batch, done))
        batch["pool"]["first_pass_complete"] = True
        self.assertIn("Skip empty pool", pool_guidance(batch, {}))
        batch["pool"]["stage_complete"] = True
        self.assertIn("Final checks", pool_guidance(batch, done))

    def test_unconfirmed_exclusion_needs_its_existing_round(self):
        decision = {"record_id": "saved", "disposition": "discard"}
        batch = {"organization": {"groups": ["A"]}, "pool": {"active_ids": ["new"]},
                 "assignments": {"excluded": decision}}
        current = {"status": "ready", "pool_mode": "current_pool", "pool_refresh_required": True,
                   "new_pool_item_ids": ["new"], "decisions": {"excluded": decision}}
        self.assertIn("confirm its exclusions", pool_guidance(batch, current))
        self.assertNotIn("confirm its exclusions", pool_guidance(batch, dict(current, closed=True)))
