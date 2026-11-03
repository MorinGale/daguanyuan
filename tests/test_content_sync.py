"""Recovery publication boundaries; no tests claim live upstream coverage."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from local_app import LocalApp
from scripts.sync_original_content import SourceError, Sync, unwrap, write_json


class ContentRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.q1 = {"id": 1, "stem": "原题", "category_id": 10, "category_ids": [10]}
        self.q2 = {"id": 2, "stem": "新增原题", "category_id": 10, "category_ids": [10], "is_core": True}
        for name in ("complete", "exam", "mock"):
            write_json(self.root / f"scrape/{name}.json", {"items": [self.q1] if name == "complete" else []})
        self.sync = Sync(self.root, delay=0)
        self.meta = {"current_revision": 1, "item_count": 2, "updated_at": "2026-10-15"}
        self.roots = [{"id": "10", "label": "高等数学", "children": []}]

    def tearDown(self):
        self.temp.cleanup()

    def answers(self, rows=None, total=2):
        return [self.meta, {"roots": self.roots}, {"items": rows or [self.q1, self.q2], "total": total}, self.meta]

    def test_complete_capture_activates_without_overwriting_source_or_learning(self):
        before = (self.root / "scrape/complete.json").read_bytes()
        app = LocalApp(self.root)
        app.dispatch("PUT", "/api/local/questions/1/state", body={"favorite": True, "note": "我的笔记"})
        with patch.object(self.sync, "get", side_effect=self.answers()):
            self.assertEqual(self.sync.bank("complete")["questions"], 2)
        app = LocalApp(self.root)
        self.assertEqual(len(app.questions), 2)
        self.assertEqual(app.list_ids["core"], {2})
        self.assertIn("mirror", app.questions[2]["content_provenance"]["canonical_capture"])
        self.assertTrue(app.dispatch("GET", "/api/local/questions/1")[1]["data"]["state"]["favorite"])
        self.assertEqual((self.root / "scrape/complete.json").read_bytes(), before)
        self.assertEqual(app.directory_view({})["complete"][0]["local_question_count"], 2)

    def test_incomplete_or_duplicate_pages_keep_prior_capture(self):
        target = self.root / "mirror/recovered/questions/complete.json"
        target.parent.mkdir(parents=True)
        target.write_text("previous capture", encoding="utf-8")
        cases = [self.answers([self.q1, self.q1]),
                 self.answers(total=3),
                 [self.meta, {"roots": self.roots}, {"items": [self.q1], "total": 2}, {"items": [], "total": 2}]]
        for replies in cases:
            with self.subTest(replies=replies), patch.object(self.sync, "get", side_effect=replies):
                with self.assertRaises(SourceError):
                    self.sync.bank("complete")
                self.assertEqual(target.read_text(), "previous capture")

    def test_moving_revision_never_activates(self):
        replies = self.answers()
        replies[-1] = {**self.meta, "current_revision": 2}
        with patch.object(self.sync, "get", side_effect=replies), self.assertRaises(SourceError):
            self.sync.bank("complete")
        self.assertFalse((self.root / "mirror/recovered/questions/complete.json").exists())

    def knowledge_fixture(self):
        write_json(self.root / "app/data/knowledge.json", {"catalog": [{"chapters": [{"items": [
            {"conceptCode": "pgmath.c01-01"}, {"conceptCode": "pgmath.c01-02"}]}]}]})
        path = self.root / "app/data/knowledge-content.json"
        prior = {"schema_version": 1, "contents": {"pgmath.c00": {"code": "pgmath.c00", "name": "已保存", "content_markdown": "真实旧正文"}}}
        write_json(path, prior)
        return path

    def test_maintenance_preserves_knowledge_and_is_not_empty_success(self):
        path = self.knowledge_fixture()
        before = path.read_bytes()
        with patch.object(self.sync, "get", side_effect=SourceError("source_http_503", 503)):
            result = self.sync.knowledge()
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["captured_this_run"], 0)
        self.assertEqual(path.read_bytes(), before)

    def test_knowledge_identity_and_partial_progress_preserve_existing(self):
        path = self.knowledge_fixture()
        content = {"code": "pgmath.c01-01", "name": "函数", "content_markdown": "# 函数\n\n原站正文 $f(x)$"}
        self.assertEqual(unwrap(content), content)
        with patch.object(self.sync, "get", side_effect=[content, SourceError("source_http_503", 503)]):
            result = self.sync.knowledge()
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["available"], 2)
        saved = json.loads(path.read_text(encoding="utf-8"))["contents"]
        self.assertEqual(saved[content["code"]], content)
        self.assertIn("pgmath.c00", saved)
        self.assertTrue(list((self.root / "backups/content-sync").rglob("knowledge-content.json")))

    def test_wrong_concept_cannot_be_published_under_requested_code(self):
        path = self.knowledge_fixture()
        before = path.read_bytes()
        with patch.object(self.sync, "get", return_value={"code": "wrong", "name": "wrong", "content_markdown": "wrong"}):
            self.assertEqual(self.sync.knowledge()["status"], "blocked")
        self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
