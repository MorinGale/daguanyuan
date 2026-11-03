"""Exercise the real publisher CLI against retained DOM and filesystem boundaries."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/publish_knowledge_dom.py"


class PublishKnowledgeDomTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="daguan-knowledge-publish-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        source_catalog = json.loads((ROOT / "app/data/knowledge.json").read_text(encoding="utf-8"))
        tree = copy.deepcopy(source_catalog["catalog"][0])
        tree["chapters"] = tree["chapters"][:1]
        tree["chapters"][0]["items"] = tree["chapters"][0]["items"][:3]
        self.points = tree["chapters"][0]["items"]
        self.write("app/data/knowledge.json", {"schema_version": 1, "catalog": [tree]})
        self.capture = json.loads((ROOT / "raw/knowledge-dom/pgmath.c01-01.json").read_text(encoding="utf-8"))
        self.write("raw/knowledge-dom/pgmath.c01-01.json", self.capture)
        self.write("raw/knowledge-dom/links.json", {"chapters": [{"url": "/math/knowledge?chapter=C01"}],
                                                   "points": [{"url": "/math/knowledge?chapter=C01&point=C01.01"}]})

    def write(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def run_publisher(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "--root", str(self.root)],
                                text=True, capture_output=True, check=True)
        summary = json.loads(result.stdout)
        report = json.loads((self.root / "raw/knowledge-dom/coverage.json").read_text(encoding="utf-8"))
        return summary, report

    def content(self):
        return json.loads((self.root / "app/data/knowledge-content.json").read_text(encoding="utf-8"))

    def test_real_dom_publishes_exact_markdown_and_failure_history_is_deduplicated(self):
        self.write("raw/knowledge-dom/deferred.json", [
            {"url": self.capture["url"], "observed": "loading failed before successful capture"},
            {"url": "https://cxyonly.fans/math/knowledge?chapter=C01&point=C01.02&view=explanation", "observed": "loading failed"},
            {"url": "https://cxyonly.fans/math/knowledge?chapter=C01&point=C01.02&view=explanation", "observed": "retry failed"},
        ])
        summary, report = self.run_publisher()
        self.assertEqual((summary["saved"], summary["failed"], summary["unattempted"]), (1, 1, 1))
        entry = self.content()["contents"]["pgmath.c01-01"]
        self.assertEqual(entry["content_markdown"], self.capture["content_markdown"])
        self.assertEqual(entry["tex_validation"]["annotation_count"], self.capture["formula_count"])
        self.assertTrue(entry["tex_validation"]["sequence_matches"])
        self.assertEqual(report["validated_dom_capture_count"], 1)
        self.assertIn("not original upstream Markdown", report["method"])

    def test_preserves_prior_content_backs_up_then_repeated_run_is_idempotent(self):
        old = {"schema_version": 1, "contents": {"retained": {"code": "retained", "content_markdown": "prior local content"}}}
        self.write("app/data/knowledge-content.json", old)
        before = (self.root / "app/data/knowledge-content.json").read_bytes()
        _, report = self.run_publisher()
        self.assertEqual(self.content()["contents"]["retained"], old["contents"]["retained"])
        self.assertEqual((self.root / report["backup"]).read_bytes(), before)
        published = (self.root / "app/data/knowledge-content.json").read_bytes()
        _, repeated = self.run_publisher()
        self.assertFalse(repeated["changed"])
        self.assertIsNone(repeated["backup"])
        self.assertEqual((self.root / "app/data/knowledge-content.json").read_bytes(), published)
        self.assertEqual(len(list((self.root / "backups/knowledge-dom").rglob("knowledge-content.json"))), 1)
        self.assertFalse(list(self.root.rglob("*.part")))

    def test_wrong_url_and_changed_tex_are_rejected_without_overwriting_good_content(self):
        self.run_publisher()
        published = (self.root / "app/data/knowledge-content.json").read_bytes()
        changed = copy.deepcopy(self.capture)
        changed["url"] = changed["url"].replace("point=C01.01", "point=C01.02")
        self.write("raw/knowledge-dom/pgmath.c01-01.json", changed)
        _, report = self.run_publisher()
        self.assertEqual(report["rejected_captures"][0]["reason"], "catalog_url_identity_mismatch")
        self.assertEqual((self.root / "app/data/knowledge-content.json").read_bytes(), published)
        changed = copy.deepcopy(self.capture)
        changed["content_markdown"] = changed["content_markdown"].replace("f:D\\to\\mathbb R", "f:D\\to\\mathbb Q", 1)
        self.write("raw/knowledge-dom/pgmath.c01-01.json", changed)
        _, report = self.run_publisher()
        self.assertEqual(report["rejected_captures"][0]["reason"], "tex_annotation_sequence_mismatch")
        self.assertEqual((self.root / "app/data/knowledge-content.json").read_bytes(), published)

    def test_broken_existing_file_is_not_silently_replaced(self):
        path = self.root / "app/data/knowledge-content.json"
        path.write_text("{incomplete", encoding="utf-8")
        result = subprocess.run([sys.executable, str(SCRIPT), "--root", str(self.root)], capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(path.read_text(encoding="utf-8"), "{incomplete")

    def test_annotation_own_leading_space_is_preserved(self):
        capture = copy.deepcopy(self.capture)
        capture["html"] = capture["html"].replace('encoding="application/x-tex">f:D', 'encoding="application/x-tex"> f:D', 1)
        capture["content_markdown"] = capture["content_markdown"].replace('$f:D', '$ f:D', 1)
        self.write("raw/knowledge-dom/pgmath.c01-01.json", capture)
        _, report = self.run_publisher()
        self.assertFalse(report["rejected_captures"])
        self.assertIn('$ f:D', self.content()["contents"]["pgmath.c01-01"]["content_markdown"])


if __name__ == "__main__":
    unittest.main()
