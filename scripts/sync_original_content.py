#!/usr/bin/env python3
"""Recover original content through the published, read-only public endpoints.

Original scrape files and personal learning data are never overwritten. A bank
is activated only after every page, revision and directory has been checked.
Maintenance/auth/rate-limit responses stop the run; they are not empty content.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, build_opener

if __package__:
    from .recover_assets import PublicOnlyRedirects, collect_references, recover, preserve_manifest_provenance
else:
    from recover_assets import PublicOnlyRedirects, collect_references, recover, preserve_manifest_provenance

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "https://cxyonly.fans"


class SourceError(ValueError):
    def __init__(self, reason, status=None):
        super().__init__(reason)
        self.status = status


def unwrap(value):
    if not isinstance(value, dict):
        raise SourceError("expected_object_response")
    if type(value.get("code")) is int:
        if value["code"] != 0 or not isinstance(value.get("data"), dict):
            raise SourceError("unsuccessful_api_envelope")
        return value["data"]
    return value


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class Sync:
    def __init__(self, root=ROOT, timeout=20, delay=0.4):
        self.root, self.timeout, self.delay = Path(root), timeout, delay
        self.stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        self.run = self.root / "raw/content-sync" / self.stamp
        self.run.mkdir(parents=True)
        self.requests = []
        self.sections = {}
        self.opener = build_opener(PublicOnlyRedirects())

    def get(self, path, label):
        # Only observed same-origin read routes are accepted; no ambient cookies
        # or authentication credentials are loaded, logged or persisted.
        if not path.startswith("/api/") or urlsplit(path).netloc:
            raise SourceError("unsupported_source_path")
        url = ORIGIN + path
        status, content_type, body = None, None, b""
        try:
            request = Request(url, headers={"Accept": "application/json", "User-Agent": "DaguanyuanPersonalMirror/1.0"})
            with self.opener.open(request, timeout=self.timeout) as response:
                status, content_type = response.status, response.headers.get_content_type()
                body = response.read(16 * 1024 * 1024 + 1)
        except HTTPError as exc:
            status, content_type, body = exc.code, exc.headers.get_content_type(), exc.read(65536)
        except (URLError, TimeoutError, OSError) as exc:
            self.requests.append({"url": url, "status": None, "error": type(exc).__name__})
            raise SourceError(f"transport_error:{type(exc).__name__}") from exc
        saved = self.run / (label + ".response")
        saved.write_bytes(body)
        self.requests.append({"url": url, "status": status, "content_type": content_type,
                              "path": saved.relative_to(self.root).as_posix(),
                              "sha256": hashlib.sha256(body).hexdigest()})
        if status != 200:
            raise SourceError(f"source_http_{status}", status)
        if len(body) > 16 * 1024 * 1024 or content_type != "application/json":
            raise SourceError("unexpected_response_size_or_type")
        try:
            value = json.loads(body)
        except (ValueError, UnicodeError) as exc:
            raise SourceError("invalid_json_response") from exc
        if self.delay:
            time.sleep(self.delay)
        return unwrap(value)

    def publish(self, relative, value):
        destination = self.root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            backup = self.root / "backups/content-sync" / self.stamp / relative
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(destination, backup)
        temporary = destination.with_suffix(destination.suffix + ".part")
        write_json(temporary, value)
        temporary.replace(destination)

    def bank(self, name):
        metadata = self.get(f"/api/question-lists/{name}", name + "-metadata")
        revision = metadata.get("current_revision", metadata.get("revision"))
        expected = metadata.get("item_count")
        if type(revision) is not int or revision < 1 or type(expected) is not int or expected < 0:
            raise SourceError("invalid_list_revision_or_count")
        directory = self.get(f"/api/question-lists/{name}/directory?revision={revision}", name + "-directory")
        if not isinstance(directory.get("roots"), list):
            raise SourceError("invalid_directory")
        items, seen, page = [], set(), 1
        while True:
            query = urlencode({"question_list_id": name, "question_list_revision": revision,
                               "page": page, "per_page": 200})
            result = self.get("/api/questions?" + query, f"{name}-page-{page}")
            rows = result.get("items")
            if type(result.get("total")) is not int or result["total"] != expected or not isinstance(rows, list):
                raise SourceError("list_count_disagrees_with_pages")
            if result.get("page", page) != page:
                raise SourceError("unexpected_page_identity")
            for q in rows:
                if not isinstance(q, dict) or type(q.get("id")) is not int or q["id"] < 1 or q["id"] in seen:
                    raise SourceError("duplicate_or_invalid_question_identity")
                if not (q.get("stem") or (q.get("document") or {}).get("stem_md")):
                    raise SourceError("missing_question_document")
                seen.add(q["id"])
            items.extend(rows)
            if len(items) >= expected:
                break
            if not rows:
                raise SourceError("incomplete_pagination")
            page += 1
        if len(items) != expected:
            raise SourceError("pagination_coverage_mismatch")
        final = self.get(f"/api/question-lists/{name}", name + "-metadata-after")
        if (final.get("current_revision", final.get("revision")), final.get("item_count"), final.get("updated_at")) != (revision, expected, metadata.get("updated_at")):
            raise SourceError("source_changed_during_capture")
        capture = {"schema_version": 1, "list_id": name, "revision": revision,
                   "fetched_at": self.stamp, "api_total": expected, "fetched": len(items),
                   "validated_complete": True, "items": items, "directory": directory,
                   "metadata": metadata, "source_run": self.run.relative_to(self.root).as_posix()}
        self.publish(f"mirror/recovered/questions/{name}.json", capture)
        return {"status": "recovered", "questions": len(items), "revision": revision}

    def knowledge(self):
        catalog = json.loads((self.root / "app/data/knowledge.json").read_text(encoding="utf-8"))["catalog"]
        destination = self.root / "app/data/knowledge-content.json"
        saved = json.loads(destination.read_text(encoding="utf-8")) if destination.exists() else {"schema_version": 1, "contents": {}}
        contents = dict(saved["contents"])
        count, missing, failure = 0, [], None
        for subject in catalog:
            for chapter in subject["chapters"]:
                for point in chapter["items"]:
                    code = point["conceptCode"]
                    try:
                        value = self.get("/api/v1/learning-concepts/" + quote(code, safe="") + "/content", "knowledge-" + code)
                        if value.get("code") != code or not isinstance(value.get("content_markdown"), str) or not value["content_markdown"].strip() or not isinstance(value.get("name"), str):
                            raise SourceError("invalid_knowledge_content_identity_or_body")
                        contents[code] = value
                        count += 1
                    except SourceError as exc:
                        if exc.status == 404:
                            missing.append(code)
                            continue
                        failure = exc
                        break
                if failure:
                    break
            if failure:
                break
        if count:
            self.publish("app/data/knowledge-content.json", {"schema_version": 1, "contents": contents,
                         "source_run": self.run.relative_to(self.root).as_posix()})
        result = {"status": "blocked" if failure else "recovered", "captured_this_run": count,
                  "available": len(contents), "not_published": missing}
        if failure:
            result["reason"] = str(failure)
        return result

    def report(self):
        value = {"observed_at": self.stamp, "origin": ORIGIN,
                 "provenance": "ordinary public HTTP captures; not REA runtime Evidence",
                 "authentication": "none", "sections": self.sections, "requests": self.requests}
        write_json(self.run / "report.json", value)
        write_json(self.root / "raw/content-sync/latest.json", value)
        return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sections", nargs="+", choices=("questions", "knowledge", "images"), default=["questions", "knowledge", "images"])
    args = parser.parse_args()
    sync = Sync()
    unavailable = None
    for section in args.sections:
        if unavailable:
            sync.sections[section] = {"status": "deferred", "reason": unavailable}
            continue
        try:
            if section == "questions":
                sync.sections[section] = {"status": "running", "banks": {}}
                for name in ("complete", "exam", "mock"):
                    sync.sections[section]["banks"][name] = sync.bank(name)
                sync.sections[section]["status"] = "recovered"
            elif section == "knowledge":
                sync.sections[section] = sync.knowledge()
                if sync.sections[section]["status"] == "blocked":
                    unavailable = sync.sections[section]["reason"]
            else:
                assets, sources = collect_references()
                recovered, received = recover(assets, 4, 20)
                available = sum(a["status"] in ("downloaded", "existing") for a in recovered.values())
                report = {"format_version": 1, "generated_at": sync.stamp,
                             "origin": ORIGIN, "provenance_kind": "public_asset_download_manifest_not_rea_evidence",
                             "authentication": "none", "source_files": sources, "assets": recovered,
                             "summary": {"referenced": len(assets), "available": available, "missing": len(assets) - available,
                                         "bytes_received": received, "attempted": sum(a.get("attempted", False) for a in recovered.values())}}
                previous_path = sync.root / "raw/asset-recovery.json"
                previous = json.loads(previous_path.read_text(encoding="utf-8")) if previous_path.exists() else {}
                sync.publish("raw/asset-recovery.json", preserve_manifest_provenance(report, previous))
                sync.sections[section] = {"status": "recovered" if available == len(assets) else "partial", "available": available, "referenced": len(assets)}
        except SourceError as exc:
            sync.sections[section] = {**sync.sections.get(section, {}), "status": "blocked", "reason": str(exc)}
            unavailable = str(exc)
        sync.report()
    result = sync.report()
    print(json.dumps(result["sections"], ensure_ascii=False, indent=2))
    print("Report: " + str(sync.run / "report.json"))
    return 1 if unavailable or any(v.get("status") == "partial" for v in sync.sections.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
