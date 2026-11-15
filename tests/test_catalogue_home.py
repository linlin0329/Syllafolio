"""Offline directory links, family examples, and the scope shown to a recipient."""
import csv
from html.parser import HTMLParser
from pathlib import Path
from tempfile import TemporaryDirectory
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from syllafolio.annotation_catalogue_report import render_html, write_catalogue_report


class ReportIndex(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ids = []
        self.fragments = []
        self.assets = []
        self.text = []
        self.family_cards = {}
        self.current = None
        self.index = False
        self.index_links = []
        self.unexpected = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script" or any(k.startswith("on") for k in attrs):
            self.unexpected.append(tag)
        anchor = attrs.get("id")
        if anchor:
            self.ids.append(anchor)
        if tag == "nav" and anchor == "family-index":
            self.index = True
        if tag == "article" and "data-type-id" in attrs:
            self.current = attrs["data-type-id"]
            self.family_cards[self.current] = dict(anchor=anchor, recordings=[], roles=[])
        if tag == "figure" and self.current:
            self.family_cards[self.current]["roles"].append((attrs["data-syllable-id"], attrs["data-role"]))
        if tag == "audio" and self.current:
            self.family_cards[self.current]["recordings"].append((attrs["src"], "controls" in attrs))
        for k in ("src", "href"):
            if attrs.get(k):
                url = attrs[k]
                if url.startswith("#"):
                    self.fragments.append(url[1:])
                    if self.index:
                        self.index_links.append(url[1:])
                else:
                    self.assets.append(url)

    def handle_endtag(self, tag):
        if tag == "nav":
            self.index = False
        if tag == "article":
            self.current = None

    def handle_data(self, text):
        self.text.append(text)


class CatalogueHomeTests(unittest.TestCase):
    def example(self):
        name = 'Descending </script> <b>note</b>'
        family = 'F-叶/#<4>'
        doc = {
            "mode": "draft", "scope": {"declared": True, "purpose": "Compare falling calls.", "target_level": "family"},
            "counts": {"assigned": 1, "unresolved": 0, "excluded": 0, "unreviewed": 0, "families": 1},
            "catalogue": [
                {"local_type_id": "L-é", "parent_type_id": family, "member_ids": ["s2"], "scope_member_ids": ["s2"],
                 "definition_state": "missing", "card": {"title": "Variant A"}},
                {"local_type_id": "F-empty", "parent_type_id": None, "member_ids": [], "scope_member_ids": [],
                 "definition_state": "missing"},
                {"local_type_id": family, "parent_type_id": None, "member_ids": ["s1","s2"], "scope_member_ids": ["s2"],
                 "definition_state": "reviewed", "card": {"title": name, "diagnostic_body": "Falls by 2 kHz.",
                                                        "representative_ids": ["s1"]}},
            ],
            "items": [{"item_id":"s1", "in_scope":False, "family_id":family, "leaf_id":None},
                      {"item_id":"s2", "in_scope":True, "category":"assigned", "family_id":family, "leaf_id":"L-é"}],
            "media": [{"item_id": "s1", "roles": [{"type_id":family,"role":"representative"}],
                       "wav_path":"media/example.wav","spectrogram_path":"media/example.png"}],
            "assessment": {"status": "not_estimable", "stale_for_current": True,
                           "report":{"status":"not_estimable","reasons":[{"message":"Too few recordings."}]}},
        }
        return name, family, doc

    def test_family_and_leaf_links_lead_to_criteria_and_registered_audio(self):
        name, family, doc = self.example()
        page = ReportIndex()
        page.feed(render_html(doc))
        self.assertFalse(page.unexpected)
        self.assertEqual(len(page.ids), len(set(page.ids)))
        self.assertLessEqual(set(page.fragments), set(page.ids))
        self.assertEqual(page.index_links, [page.family_cards[k]["anchor"] for k in (family, "L-é", "F-empty")])
        self.assertEqual(page.family_cards[family]["recordings"], [("media/example.wav", True)])
        self.assertEqual(page.family_cards[family]["roles"], [("s1", "representative")])
        self.assertFalse(page.family_cards["L-é"]["recordings"])
        self.assertIn(name, page.text)
        self.assertIn("Falls by 2 kHz.", page.text)
        self.assertIn("Outside scope · reference example", "".join(page.text))
        self.assertIn("1 outside the scope", "".join(page.text))
        self.assertIn("historical", "".join(page.text).lower())
        self.assertIn("media/example.png", page.assets)

    def test_name_and_stable_id_are_both_available_in_scope_csv(self):
        name, family, doc = self.example()
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_catalogue_report(root, doc)
            with (root/"tables/scope_items.csv").open(encoding="utf-8-sig", newline="") as f:
                row, = csv.DictReader(f)
            self.assertEqual((row["item_id"], row["family_id"], row["family_name"]), ("s2",family,name))
            self.assertEqual((row["leaf_id"], row["leaf_name"]), ("L-é","Variant A"))
