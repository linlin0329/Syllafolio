"""Keep reviewed outcomes distinct from actionable blockers in Final checks."""
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from syllafolio.annotation_desktop_final_review import FinalReviewController, FinalReviewError


class FinalCheckActions(unittest.TestCase):
    def setUp(self):
        self.rows = [{"item_id": ident, "processing_state": status, "batch_disposition": outcome}
                     for ident, status, outcome in (
                         ("a", "decided", "current"), ("b", "reviewed_unresolved", "pool"),
                         ("c", "decided", "discard"), ("d", "unfinished", None),
                         ("e", "reviewed_unresolved", "pool"))]
        self.state = {
            "items": self.rows, "revision": 1, "catalogue_scope": {"scope_id": "s"},
            "memberships": [{"local_type_id": "g", "parent_type_id": None, "member_ids": ["a"]}],
            "type_cards": {"g": {"title": "Chirp"}},
            "completion": {"scope_ids": ["a", "b", "c", "d"], "can_submit": False},
            "batch_review": {"reviews": {"r": {"family_id": "g", "directed_issue_id": "issue"}},
                             "active_family_reviews": {"g": "r"},
                             "organization": {"groups": [{"group_id": "cg", "label": "c_0001"}]}},
            "issues": {"issue": {"target_ids": ["a"], "directed_review_id": "r"}},
            "final_review": {"gaps": [], "stages": {"first": {"pending_ids": ["cg"]}}},
        }
        project = SimpleNamespace(metadata={"project_id": "p"}, service=SimpleNamespace(run_id="run"),
                                  dataset=SimpleNamespace(input_fingerprint="audio"), snapshot=lambda: self.state)
        self.controller = FinalReviewController(project)

    def view(self, gaps):
        self.state["final_review"]["gaps"] = gaps
        return self.controller.snapshot()

    def test_scoped_outcomes_and_real_unfinished_items(self):
        got = self.view([{"code": "UNFINISHED_ITEMS", "item_ids": ["d"],
                          "location": {"family_ids": ["g"]}},
                         {"code": "BATCH_REVIEW_OPEN", "review_ids": ["r"]}])
        self.assertEqual(got["outcome_counts"], {"assigned": 1, "reviewed_unresolved": 1,
                                                 "excluded": 1, "unfinished": 1})
        self.assertEqual([[t["id"] for t in gap["targets"]] for gap in got["gaps"]], [["d"], ["r"]])
        self.state["final_review"]["gaps"] = []
        with self.assertRaises(FinalReviewError):
            self.controller.resolve_location("item", "d")

    def test_routes_for_setup_pool_definitions_and_jobs(self):
        got = self.view([{"code": "SCOPE_REQUIRED"}, {"code": "BATCH_FIRST_PASS_PENDING"},
                         {"code": "BATCH_POOL_PENDING"},
                         {"code": "STALE_TYPE_DEFINITIONS", "type_ids": ["g"]},
                         {"code": "ACTIVE_JOBS", "job_ids": ["worker"]},
                         {"code": "BATCH_LEAF_UNASSESSED", "item_ids": ["a"]}])
        routes = [[(t["kind"], t["id"]) for t in gap["targets"]] for gap in got["gaps"]]
        self.assertEqual(routes, [[("scope", "scope")], [("group", "cg")], [("pool", "pool")],
                                  [("type", "g")], [("tasks", "tasks")], [("fine", "a")]])

    def test_one_action_per_type_card_even_with_two_blockers(self):
        self.state["memberships"].append({"local_type_id": "h", "parent_type_id": None, "member_ids": []})
        missing = [{"code": "TYPE_DEFINITIONS", "message": "A card needs review.", "type_ids": ["g"]},
                   {"code": "STALE_TYPE_DEFINITIONS", "message": "Membership changed.", "type_ids": ["g", "h"]}]
        self.state["completion"]["missing"] = missing
        view = self.view(missing)
        self.assertEqual(view["completion"]["missing"], missing)
        task, = view["gaps"]
        self.assertEqual(task["codes"], [g["code"] for g in missing])
        self.assertIn("Membership changed.", task["message"])
        self.assertEqual([t["id"] for t in task["targets"]], ["g", "h"])

    def test_directed_round_has_priority_when_that_is_what_blocks_pool_coverage(self):
        criteria = {"code": "DEFINITION_REVIEW_REQUIRED", "type_ids": ["g"],
                    "location": {"family_ids": ["g"]}}
        got = self.view([criteria, {"code": "BATCH_POOL_PENDING", "review_ids": ["r"]},
                         {"code": "OPEN_REVIEWS", "issue_ids": ["issue"]}])
        self.assertTrue(all([(t["kind"], t["id"]) for t in gap["targets"]] == [("review", "r")]
                            for gap in got["gaps"]))
        self.state["batch_review"]["reviews"]["r"]["closed"] = True
        target, = self.view([criteria])["gaps"][0]["targets"]
        self.assertEqual((target["kind"], target["id"]), ("directed", "g"))

    def test_superseded_open_pool_directs_to_actual_recovery_scope(self):
        self.state["batch_review"].update(pool={"active_ids": ["b"]})
        self.state["batch_review"]["reviews"]["old"] = {"stage": "pool", "read_only": True, "closed": False}
        for row in self.rows:
            row.update(batch_review_id="old", closure_confirmation_required=True)
        gap = self.view([{"code": "BATCH_REVIEW_OPEN", "review_ids": ["old"]}])["gaps"][0]
        self.assertEqual({(t["kind"], t["id"]) for t in gap["targets"]}, {("pool","pool"), ("family","g")})
        self.assertIn("checkpoint", gap["message"])
        self.assertIn("c", gap["message"])
        self.assertEqual(self.controller.resolve_location("family", "g")["kind"], "family")
