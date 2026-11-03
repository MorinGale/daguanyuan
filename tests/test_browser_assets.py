"""Local image imports preserve failed fetch evidence and verify captured identity."""
import base64
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from io import StringIO
import copy

from scripts.import_browser_assets import import_assets
from scripts import recover_assets, sync_original_content


class BrowserAssetImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.incoming = self.root / "raw/question-image-dom"
        self.incoming.mkdir(parents=True)
        self.data = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")
        self.digest = hashlib.sha256(self.data).hexdigest()
        self.filename = self.digest + ".png"
        (self.incoming / self.filename).write_bytes(self.data)
        self.path = self.root / "raw/asset-recovery.json"
        self.manifest = {"generated_at": "2026-10-14T00:00:00Z", "provenance_kind": "public_download",
                         "authentication": "none", "summary": {"available": 0, "missing": 1, "attempted": 1, "bytes_received": 0},
                         "assets": {self.digest: {"content_sha256": self.digest, "source_url": "https://cxyonly.fans/api/v1/question-assets/" + self.digest,
                                                  "sources": [{"question_id": 1643}], "status": "failed", "attempted": True,
                                                  "http_status": 401, "reason": "authentication_required"}}}
        self.path.write_text(json.dumps(self.manifest), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_import_verified_bytes_retains_failed_attempt_and_is_idempotent(self):
        result = import_assets(self.root, "https://cxyonly.fans/math/questions/1643", [self.filename])
        self.assertEqual((result["imported"], result["available"]), (1, 1))
        manifest = json.loads(self.path.read_text(encoding="utf-8"))
        record = manifest["assets"][self.digest]
        self.assertEqual((record["status"], record["content_type"]), ("existing", "image/png"))
        self.assertEqual((self.root / "mirror" / record["path"]).read_bytes(), self.data)
        self.assertEqual(record["recovery_history"][0]["http_status"], 401)
        self.assertEqual(record["browser_dom_provenance"][0]["question_id"], 1643)
        self.assertEqual(manifest["summary"]["bytes_received"], 0)
        self.assertEqual(json.loads((Path(result["backup_path"]) / "asset-recovery.json").read_text(encoding="utf-8")), self.manifest)
        before = self.path.read_bytes()
        self.assertFalse(import_assets(self.root, "https://cxyonly.fans/math/questions/1643")["changed"])
        self.assertEqual(self.path.read_bytes(), before)

    def test_wrong_page_or_unreferenced_image_does_not_mutate_manifest(self):
        before = self.path.read_bytes()
        for url in ("https://example.org/math/questions/1643", "https://cxyonly.fans/math/questions/1"):
            with self.assertRaises(ValueError):
                import_assets(self.root, url)
        manifest = dict(self.manifest, assets={})
        self.path.write_text(json.dumps(manifest), encoding="utf-8")
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            import_assets(self.root, "https://cxyonly.fans/math/questions/1643")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse((self.root / "backups").exists())

    def test_digest_mime_and_directory_escapes_are_rejected_before_writing(self):
        before = self.path.read_bytes()
        (self.incoming / self.filename).write_bytes(self.data + b"changed")
        with self.assertRaises(ValueError):
            import_assets(self.root, "https://cxyonly.fans/math/questions/1643")
        (self.incoming / self.filename).write_bytes(self.data)
        wrong_mime = self.digest + ".jpg"
        (self.incoming / wrong_mime).write_bytes(self.data)
        for filenames in ([wrong_mime], ["../" + self.filename]):
            with self.assertRaises(ValueError):
                import_assets(self.root, "https://cxyonly.fans/math/questions/1643", filenames)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse((self.root / "mirror").exists())

    def test_observation_saved_entries_are_checked_and_failed_entries_are_ignored(self):
        observation = {"question_id": 1643, "source_url": "https://cxyonly.fans/math/questions/1643",
                       "sha256": self.digest, "bytes": len(self.data), "observed_at": "2026-10-15T06:00:00Z"}
        path = self.incoming / "observations.json"
        path.write_text(json.dumps({"saved": {self.digest: observation}, "failed": [{"sha256": "0" * 64}]}), encoding="utf-8")
        for field, value in (("sha256", "0" * 64), ("question_id", 940), ("bytes", 1)):
            broken = copy.deepcopy(observation)
            broken[field] = value
            path.write_text(json.dumps({"saved": {self.digest: broken}}), encoding="utf-8")
            with self.assertRaises(ValueError):
                import_assets(self.root, observations="observations.json")
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["summary"]["available"], 0)
        path.write_text(json.dumps({"saved": {self.digest: observation}, "failed": [{"sha256": "0" * 64}]}), encoding="utf-8")
        self.assertEqual(import_assets(self.root, observations="observations.json")["imported"], 1)
        self.assertFalse(import_assets(self.root, observations="observations.json")["changed"])
        record = json.loads(self.path.read_text(encoding="utf-8"))["assets"][self.digest]
        self.assertEqual(record["browser_dom_provenance"][0]["observation"], observation)

    def test_both_public_writers_retain_verified_browser_provenance_without_network(self):
        import_assets(self.root, "https://cxyonly.fans/math/questions/1643")
        original = json.loads(self.path.read_text(encoding="utf-8"))
        refs = {self.digest: {key: self.manifest["assets"][self.digest][key]
                             for key in ("content_sha256", "source_url", "sources")}}
        class OfflineOpener:
            def open(self, *args, **kwargs):
                raise AssertionError("Verified existing images must never send requests")
        with patch.object(recover_assets, "ROOT", self.root), patch.object(recover_assets.urllib.request, "build_opener", return_value=OfflineOpener()):
            with patch.object(recover_assets, "collect_references", return_value=(refs, [])), patch("sys.argv", ["recover_assets"]), patch("sys.stdout", new=StringIO()):
                recover_assets.main()
            after_public = json.loads(self.path.read_text(encoding="utf-8"))
            self.assertEqual(after_public["assets"][self.digest]["browser_dom_provenance"], original["assets"][self.digest]["browser_dom_provenance"])
            self.assertEqual(after_public["assets"][self.digest]["recovery_history"], original["assets"][self.digest]["recovery_history"])
            self.assertEqual(after_public["original_download_provenance"], original["original_download_provenance"])
            isolated_sync = sync_original_content.Sync(root=self.root)
            with patch.object(sync_original_content, "Sync", return_value=isolated_sync), patch.object(sync_original_content, "collect_references", return_value=(refs, [])), patch("sys.argv", ["sync_original_content", "--sections", "images"]), patch("sys.stdout", new=StringIO()):
                self.assertEqual(sync_original_content.main(), 0)
        after_sync = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(after_sync["assets"][self.digest]["browser_dom_provenance"], original["assets"][self.digest]["browser_dom_provenance"])
        self.assertEqual(after_sync["assets"][self.digest]["recovery_history"], original["assets"][self.digest]["recovery_history"])
        self.assertEqual(after_sync["summary"]["browser_dom_available"], 1)
        self.assertEqual(after_sync["original_download_provenance"], original["original_download_provenance"])


if __name__ == "__main__":
    unittest.main()
