"""Exercise personal workflows at the same dispatch boundary as HTTP."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from local_app import LocalApp, TZ, STANDARD_PAPER_RULES


class PersonalWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "scrape").mkdir()
        (self.root / "mirror" / "api-data").mkdir(parents=True)
        self.q1 = {"id": 1, "category_id": 11, "category_ids": [11], "stem": "求函数极限", "source": "2025真题", "question_type": "single_choice", "document": {"stem_md": "求函数极限", "answer": {"option_ids": ["A"]}}}
        exam_q1 = copy.deepcopy(self.q1)
        exam_q1["source"] = "跨题单版本来源"
        exam_q1["document"]["stem_md"] = "同题号的另一文档版本"
        q2 = {"id": 2, "category_ids": [12], "stem": "积分", "source": "习题", "question_type": "subjective"}
        q3 = {"id": 3, "category_ids": [999], "stem": "未知科目", "question_type": "subjective"}
        for name, questions in {"complete": [self.q1, q2], "exam": [exam_q1, q3], "mock": []}.items():
            (self.root / "scrape" / f"{name}.json").write_text(json.dumps({"items": questions}), encoding="utf-8")
            nodes = [{"id": "10", "label": "高等数学", "children": [{"id": "11", "label": "函数极限", "children": []}, {"id": "12", "label": "积分", "children": []}]}]
            (self.root / "mirror" / "api-data" / f"dir-{name}.json").write_text(json.dumps({"data": {"roots": nodes}}), encoding="utf-8")
        categories = [{"id": 10, "name": "高等数学", "effective_exam_subjects": ["math_1"], "children": [{"id": 11, "name": "函数极限", "children": []}, {"id": 12, "name": "积分", "effective_exam_subjects": ["math_2"], "children": []}]}]
        (self.root / "mirror" / "api-data" / "categories-complete-stats.json").write_text(json.dumps(categories), encoding="utf-8")
        self.app = LocalApp(self.root, self.root / "personal")

    def tearDown(self):
        self.temp.cleanup()

    def call(self, method, path, params=None, body=None, status=200):
        actual, result = self.app.dispatch(method, "/api/local/" + path, params, body)
        self.assertEqual(actual, status, result)
        return result["data"]

    def test_counts_dedup_subtree_search_and_subject_uncertainty(self):
        bootstrap = self.call("GET", "bootstrap")
        self.assertEqual(bootstrap["total"], 3)
        self.assertEqual(bootstrap["list_counts"], {"complete": 2, "core": 0, "exam": 2, "mock": 0})
        self.assertEqual(bootstrap["directories"]["complete"][0]["local_question_count"], 2)
        self.assertEqual(self.call("GET", "questions", {"category": "10"})["total"], 2)
        self.assertEqual(self.call("GET", "questions", {"q": "高等数学"})["total"], 2)
        self.assertEqual([q["id"] for q in self.call("GET", "questions", {"subject": "math_1"})["items"]], [1, 3])
        self.assertEqual([q["id"] for q in self.call("GET", "questions", {"subject": "math_2"})["items"]], [2, 3])
        self.assertEqual(self.call("GET", "questions/1")["list_ids"], ["complete", "exam"])
        q = self.call("GET", "questions/1")
        self.assertEqual(q["document"], self.q1["document"])
        self.assertTrue(q["content_provenance"]["has_document_variants"])
        self.assertEqual(q["content_provenance"]["canonical_list"], "complete")
        self.assertEqual(q["content_provenance"]["variants"][1]["document_matches_canonical"], False)
        self.assertEqual(self.call("GET", "questions", {"q": "跨题单版本来源"})["total"], 1)
        self.assertEqual(bootstrap["type_counts"], {"single_choice": 1, "subjective": 2})
        for query in ("1", "#1"):
            self.assertEqual([q["id"] for q in self.call("GET", "questions", {"q": query})["items"]], [1])
        self.assertEqual(self.call("GET", "questions", {"q": "2025"})["total"], 1)

    def test_year_search_and_explicit_ids_keep_list_filters(self):
        complete_path = self.root / "scrape/complete.json"
        complete = json.loads(complete_path.read_text(encoding="utf-8"))
        complete["items"][1]["original_source"] = "1999年真题"
        complete["items"].extend([
            {"id": 2025, "category_ids": [11], "stem": "题号恰似年份", "source": "普通习题"},
            {"id": 3356, "category_ids": [11], "stem": "常规题号", "source": "普通习题"},
        ])
        complete_path.write_text(json.dumps(complete), encoding="utf-8")
        exam_path = self.root / "scrape/exam.json"
        exam = json.loads(exam_path.read_text(encoding="utf-8"))
        exam["items"][0]["source"] = "2024年真题的题单版本"
        exam["items"][1]["source"] = "2025年数学真题"
        exam_path.write_text(json.dumps(exam), encoding="utf-8")
        self.app = LocalApp(self.root, self.root / "personal")

        def ids(query, list_name="all"):
            return [q["id"] for q in self.call("GET", "questions", {"q": query, "list": list_name})["items"]]

        self.assertEqual(ids("2025"), [1, 3])
        self.assertEqual(ids(" 2025 ", "complete"), [1])
        self.assertEqual(ids("2025", "mock"), [])
        self.assertEqual(ids("1999"), [2])
        self.assertEqual(ids("2024"), [1])
        self.assertEqual(ids("2099"), [])
        self.assertEqual(ids("#2025"), [2025])
        self.assertEqual(ids("#2025", "exam"), [])
        self.assertEqual(ids("3356"), [3356])
        self.assertEqual(ids("#3356"), [3356])
        self.assertEqual(ids("3356", "exam"), [])

    def test_state_persists_and_source_is_immutable(self):
        self.call("PUT", "questions/1/state", body={"favorite": True, "note": "重点复习", "mastery": "mastered"})
        self.app = LocalApp(self.root, self.root / "personal")
        q = self.call("GET", "questions/1")
        self.assertEqual(q["state"]["note"], "重点复习")
        self.assertTrue(q["state"]["favorite"])
        self.assertEqual(self.call("GET", "questions", {"filter": "favorites"})["total"], 1)
        self.assertEqual(self.call("GET", "bootstrap")["directories"]["complete"][0]["mastered_count"], 1)
        self.assertEqual(self.call("GET", "questions", {"q": "重点复习"})["total"], 1)
        self.assertNotIn("state", json.loads((self.root / "scrape/complete.json").read_text())["items"][0])
        with patch.dict(os.environ, {"DGU_DATA_DIR": str(self.root / "separate")}):
            separate = LocalApp(self.root)
            self.assertFalse(separate.dispatch("GET", "/api/local/questions/1")[1]["data"]["state"]["favorite"])

    def test_core_scope_uses_saved_flags_across_captures_and_shared_state(self):
        for name, core_ids in (("complete", {1}), ("exam", {1, 3})):
            source = self.root / "scrape" / f"{name}.json"
            captured = json.loads(source.read_text(encoding="utf-8"))
            for q in captured["items"]:
                q["is_core"] = q["id"] in core_ids
            source.write_text(json.dumps(captured), encoding="utf-8")
        self.app = LocalApp(self.root, self.root / "personal")
        bootstrap = self.call("GET", "bootstrap")
        self.assertEqual(bootstrap["list_counts"]["core"], 2)
        self.assertEqual(self.call("GET", "questions", {"list": "core"})["total"], 2)
        self.assertEqual([q["id"] for q in self.call("GET", "questions", {"list": "core", "category": "10"})["items"]], [1])
        roots = bootstrap["directories"]["core"]
        self.assertEqual(roots[0]["local_question_count"], 1)
        self.assertEqual(roots[0]["children"][1]["local_question_count"], 0)
        self.assertEqual(self.call("GET", "questions", {"list": "core", "q": "#2"})["total"], 0)
        self.assertEqual(self.call("GET", "questions", {"list": "core", "q": "#3"})["total"], 1)
        self.call("PUT", "questions/1/state", body={"mastery": "mastered", "favorite": True})
        self.assertEqual(self.call("GET", "bootstrap")["directories"]["core"][0]["mastered_count"], 1)
        self.assertEqual(self.call("GET", "questions", {"list": "core", "filter": "favorites"})["total"], 1)
        paper = self.call("POST", "paper", body={"list": "core", "count": 10})
        self.assertEqual({q["id"] for q in paper["items"]}, {1, 3})
        self.assertNotIn("core", [p["list"] for p in self.app.questions[1]["content_provenance"]["variants"]])
        self.assertFalse((self.root / "scrape/core.json").exists())

    def test_attempt_wrong_due_clear_and_stats(self):
        result = self.call("POST", "questions/1/attempt", body={"result": "wrong", "answer": "B", "duration_seconds": 10})
        self.assertTrue(result["state"]["wrong"])
        self.assertIsNotNone(result["state"]["due_at"])
        self.assertEqual(self.call("GET", "questions", {"filter": "wrong"})["total"], 1)
        self.assertEqual(self.call("GET", "questions", {"filter": "due"})["total"], 0)
        exported = self.call("GET", "export")
        exported["states"]["1"]["due_at"] = "2020-01-01T00:00:00+08:00"
        self.call("POST", "import", body=exported)
        self.assertEqual(self.call("GET", "questions", {"filter": "due"})["total"], 1)
        result = self.call("POST", "questions/1/attempt", body={"result": "correct"})
        self.assertFalse(result["state"]["wrong"])
        self.assertIsNone(result["state"]["due_at"])
        stats = self.call("GET", "bootstrap")["stats"]
        self.assertEqual((stats["today_attempts"], stats["practiced"], stats["correct_rate"]), (2, 1, 50))
        self.assertEqual(self.call("GET", "records", {"per_page": 1, "page": 2})["total"], 2)

    def test_import_atomic_validation_backup_and_repeated_attempts(self):
        self.call("PUT", "questions/1/state", body={"favorite": True})
        for _ in range(2):
            self.call("POST", "questions/1/attempt", body={"result": "wrong"})
        exported = self.call("GET", "export")
        broken = copy.deepcopy(exported)
        broken["states"]["1"]["favorite"] = False
        broken["records"].append({"question_id": 2, "result": "invalid"})
        self.call("POST", "import", body=broken, status=400)
        self.assertTrue(self.call("GET", "questions/1")["state"]["favorite"])
        self.assertFalse((self.root / "personal/backups").exists())
        imported = self.call("POST", "import", body=exported)
        self.assertTrue(Path(imported["backup_path"]).is_file())
        db = sqlite3.connect(imported["backup_path"])
        try:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM records").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT favorite FROM states WHERE question_id=1").fetchone()[0], 1)
        finally:
            db.close()
        self.call("POST", "import", body=exported)
        self.assertEqual(self.call("GET", "records")["total"], 2)
        self.app = LocalApp(self.root, self.root / "second")
        self.call("POST", "import", body=exported)
        self.assertEqual(self.call("GET", "records")["total"], 2)

    def test_validation_paper_settings_and_missing_question(self):
        for params in ({"page": "oops"}, {"per_page": 0}, {"category": "bad"}, {"subject": "wrong"}):
            self.call("GET", "questions", params, status=400)
        self.call("PUT", "questions/1/state", body={"favorite": "yes"}, status=400)
        self.call("POST", "questions/1/attempt", body={"result": "correct", "duration_seconds": float("nan")}, status=400)
        self.call("GET", "questions/500", status=404)
        self.call("PUT", "settings", body={"exam_date": "2025-02-31"}, status=400)
        self.call("PUT", "settings", body={"exam_date": "2026-12-20", "daily_goal": 30, "theme": "dark"})
        self.assertEqual(self.call("GET", "bootstrap")["settings"]["daily_goal"], 30)
        paper = self.call("POST", "paper", body={"type": "single_choice", "count": 20})
        self.assertEqual(paper["total"], 1)
        self.assertEqual(paper["items"][0]["id"], 1)

    def test_stale_import_keeps_newer_learning_and_blank_date_is_explicit(self):
        self.assertEqual(self.call("GET", "bootstrap")["settings"]["exam_date"], "2026-12-19")
        self.call("PUT", "settings", body={"exam_date": ""})
        self.assertEqual(self.call("GET", "bootstrap")["settings"]["exam_date"], "")
        self.call("PUT", "questions/1/state", body={"favorite": True, "note": "最新笔记"})
        exported = self.call("GET", "export")
        exported["states"]["1"].update({"favorite": False, "note": "旧笔记", "updated_at": "2020-01-01T00:00:00+08:00"})
        result = self.call("POST", "import", body=exported)
        self.assertEqual(result["states_imported"], 0)
        self.assertEqual(result["states_skipped_older"], 1)
        state = self.call("GET", "questions/1")["state"]
        self.assertTrue(state["favorite"])
        self.assertEqual(state["note"], "最新笔记")

    def test_malformed_payloads_are_400_and_do_not_mutate_learning(self):
        before = self.call("GET", "export")
        malformed_import = {"schema_version": 1, "states": {"1": {"updated_at": "9999-12-31T23:59:59-23:00"}}, "records": [], "settings": {}}
        cases = [
            ("POST", "paper", {"list": []}),
            ("POST", "paper", {"category": []}),
            ("POST", "paper", {"type": {}}),
            ("PUT", "questions/1/state", {"mastery": {}}),
            ("PUT", "questions/1/state", []),
            ("POST", "import", malformed_import),
            ("POST", "import", {"schema_version": 1, "states": {}, "records": [None], "settings": {}}),
            ("POST", "questions/1/attempt", {"result": "correct", "answer": {}}),
        ]
        for method, path, body in cases:
            with self.subTest(path=path, body=body):
                self.call(method, path, body=body, status=400)
        after = self.call("GET", "export")
        for field in ("states", "records", "settings"):
            self.assertEqual(before[field], after[field])
        self.assertFalse((self.root / "personal/backups").exists())

    def test_concurrent_dispatch_preserves_every_attempt(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.app.dispatch("POST", "/api/local/questions/1/attempt", body={"result": "correct"}), range(12)))
        self.assertTrue(all(status == 200 for status, _ in results))
        records = self.call("GET", "records", {"per_page": 200})
        self.assertEqual(records["total"], 12)
        self.assertEqual(len({record["record_key"] for record in records["items"]}), 12)
        self.assertEqual(records["items"][0]["question"]["source"], "2025真题")
        self.assertEqual(self.call("GET", "records", {"page": "9" * 100})["items"], [])
        changes = [{"favorite": True}, {"note": "并发保存笔记"}, {"mastery": "mastered"}]
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda body: self.app.dispatch("PUT", "/api/local/questions/1/state", body=body), changes))
        self.assertTrue(all(status == 200 for status, _ in results))
        state = self.call("GET", "questions/1")["state"]
        self.assertTrue(state["favorite"])
        self.assertEqual(state["note"], "并发保存笔记")
        self.assertEqual(state["mastery"], "mastered")

    def test_four_mastery_states_and_lightweight_filtered_map(self):
        for mastery in ("not_known", "learning"):
            state = self.call("PUT", "questions/1/state", body={"mastery": mastery})
            self.assertTrue(state["wrong"])
            self.assertIsNotNone(state["due_at"])
            self.assertEqual(self.call("GET", "questions", {"filter": "wrong"})["total"], 1)
        self.call("PUT", "questions/1/state", body={"favorite": True, "note": "记住此题"})
        result = self.call("GET", "map", {"list": "complete", "category": 10, "subject": "math_1", "filter": "learning"})
        self.assertEqual(result, {"items": [{"id": 1, "index": 1, "category_id": 11, "mastery": "learning", "favorite": True, "has_note": True}], "total": 1})
        for mastery in ("mastered", "not_started"):
            state = self.call("PUT", "questions/1/state", body={"mastery": mastery})
            self.assertFalse(state["wrong"])
            self.assertIsNone(state["due_at"])
        self.assertEqual(self.call("GET", "records")["total"], 0)

    def test_year_activity_and_streak_only_count_attempts(self):
        today = datetime.now(TZ).date()
        backup = self.call("GET", "export")
        for offset in (0, 0, 1, 2, 4):
            backup["records"].append({"question_id": 1, "result": "correct", "created_at": (today - timedelta(days=offset)).isoformat() + "T12:00:00+08:00"})
        self.call("POST", "import", body=backup)
        self.call("PUT", "questions/1/state", body={"mastery": "not_known"})
        stats = self.call("GET", "bootstrap")["stats"]
        self.assertEqual(stats["streak"], 3)
        self.assertEqual(stats["total_attempts"], 5)
        self.assertEqual(stats["today_attempts"], 2)
        self.assertEqual(stats["last_activity_at"], today.isoformat() + "T12:00:00+08:00")
        self.assertEqual(stats["activity_year"][0]["date"], f"{today.year}-01-01")
        self.assertEqual(stats["activity_year"][-1]["date"], f"{today.year}-12-31")
        self.assertEqual(next(day for day in stats["activity_year"] if day["date"] == today.isoformat())["attempts"], 2)

    def test_standard_paper_rules_priority_and_nonmutating_check(self):
        roots = [{"id": 10, "label": "高等数学", "children": []}, {"id": 20, "label": "线性代数", "children": []}, {"id": 30, "label": "概率统计", "children": []}]
        questions = []
        preferred_id = None
        for rule in STANDARD_PAPER_RULES:
            cid = {"advanced_math": 10, "linear_algebra": 20, "probability": 30}[rule["discipline"]]
            kind = {"choice": "single_choice", "fill": "fill", "subjective": "subjective"}[rule["kind"]]
            for _ in range(rule["count"] + 2):
                qid = len(questions) + 1
                questions.append({"id": qid, "category_id": cid, "category_ids": [cid], "question_type": kind})
                preferred_id = qid
        (self.root / "scrape/complete.json").write_text(json.dumps({"items": questions}), encoding="utf-8")
        (self.root / "mirror/api-data/dir-complete.json").write_text(json.dumps({"data": {"roots": roots}}), encoding="utf-8")
        self.app = LocalApp(self.root, self.root / "standard-test")
        self.call("PUT", f"questions/{preferred_id}/state", body={"favorite": True})
        self.call("PUT", "questions/1/state", body={"mastery": "not_known"})
        body = {"mode": "standard", "prioritize_wrong": True, "prioritize_favorites": True}
        before = self.call("GET", "export")
        checked = self.call("POST", "paper/check", body=body)
        self.assertEqual((checked["expected_total"], checked["requested_total"]), (22, 22))
        self.assertEqual(checked["shortages"], [])
        self.assertEqual(len(checked["groups"]), 9)
        self.assertIn(preferred_id, checked["candidates"])
        paper = self.call("POST", "paper", body=body)
        self.assertEqual(len({q["id"] for q in paper["items"]}), 22)
        self.assertIn(preferred_id, [q["id"] for q in paper["items"]])
        self.assertIn(1, [q["id"] for q in paper["items"]])
        after = self.call("GET", "export")
        for field in ("states", "records", "state_events", "settings"):
            self.assertEqual(before[field], after[field])

    def test_paper_shortages_custom_rules_and_invalid_rules(self):
        paper = self.call("POST", "paper/check", body={"mode": "standard"})
        self.assertEqual((paper["expected_total"], paper["requested_total"]), (2, 22))
        self.assertEqual(sum(group["missing"] for group in paper["shortages"]), 20)
        paper = self.call("POST", "paper", body={"mode": "custom", "rules": [{"discipline": "advanced_math", "kind": "choice", "count": 1}]})
        self.assertEqual([q["id"] for q in paper["items"]], [1])
        for body in ({"mode": "bad"}, {"prioritize_wrong": "yes"}, {"rules": {}}, {"rules": [None]}, {"rules": [{"count": 1, "kind": []}]}, {"rules": [{"count": 150}, {"count": 100}]}):
            self.call("POST", "paper/check", body=body, status=400)

    def test_history_dates_and_order_apply_before_pagination(self):
        for day in ("2026-10-01", "2026-10-02", "2026-10-03"):
            self.call("POST", "questions/1/attempt", body={"result": "correct"})
            with self.app.connect() as db:
                db.execute("UPDATE records SET created_at=? WHERE id=(SELECT MAX(id) FROM records)", (day + "T12:00:00+08:00",))
        result = self.call("GET", "records", {"start": "2026-10-02", "end": "2026-10-03", "sort": "old", "per_page": 1})
        self.assertEqual((result["total"], result["total_pages"]), (2, 2))
        self.assertTrue(result["items"][0]["created_at"].startswith("2026-10-02"))
        result = self.call("GET", "records", {"start": "2026-10-02", "end": "2026-10-03", "sort": "old", "per_page": 1, "page": 2})
        self.assertTrue(result["items"][0]["created_at"].startswith("2026-10-03"))
        for params in ({"start": "2026-02-30"}, {"sort": "invalid"}, {"start": "2026-10-03", "end": "2026-10-01"}):
            self.call("GET", "records", params, status=400)

    def test_favorites_sort_uses_favorite_time_not_later_note_edit(self):
        self.call("PUT", "questions/2/state", body={"favorite": True})
        self.call("PUT", "questions/1/state", body={"favorite": True})
        self.call("PUT", "questions/2/state", body={"note": "later note"})
        for order, expected in (("new", 1), ("old", 2)):
            result = self.call("GET", "questions", {"filter": "favorites", "sort": order, "per_page": 1})
            self.assertEqual(result["items"][0]["id"], expected)
            self.assertEqual(result["total"], 2)

    def test_explicit_state_event_history_is_deduplicated_and_backed_up(self):
        for _ in range(2):
            self.call("PUT", "questions/1/state", body={"favorite": True, "mastery": "learning"})
        self.call("POST", "questions/1/attempt", body={"result": "correct"})
        self.assertEqual(self.call("GET", "records")["total"], 1)
        combined = self.call("GET", "records", {"record_type": "all"})
        self.assertEqual(combined["total"], 3)
        self.assertEqual({event["event_kind"] for event in combined["items"]}, {"favorite", "mastery", "attempt"})
        self.assertEqual(self.call("GET", "records", {"record_type": "learning"})["total"], 1)
        self.call("PUT", "questions/1/state", body={"favorite": False})
        favorites = self.call("GET", "records", {"record_type": "favorite"})
        self.assertEqual(favorites["items"][0]["action"], "unfavorite")
        backup = self.call("GET", "export")
        self.app = LocalApp(self.root, self.root / "restored-events")
        for _ in range(2):
            self.call("POST", "import", body=backup)
        self.assertEqual(self.call("GET", "records", {"record_type": "all"})["total"], 4)
        self.assertEqual(self.call("GET", "bootstrap")["stats"]["total_attempts"], 1)
        broken = copy.deepcopy(backup)
        broken["state_events"][0]["new_value"] = None
        self.call("POST", "import", body=broken, status=400)


if __name__ == "__main__":
    unittest.main()
