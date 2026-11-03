"""Exercise the public HTTP boundary with isolated disposable learning data."""
import http.client
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def request(port, method, path, data=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    payload = json.dumps(data).encode() if data is not None else None
    headers = headers or {}
    if payload is not None:
        headers.setdefault("Content-Type", "application/json")
    try:
        conn.request(method, path, payload, headers)
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        conn.close()


def launch(data_dir):
    env = dict(os.environ, DGU_DATA_DIR=str(data_dir), PYTHONIOENCODING="utf-8")
    process = subprocess.Popen([sys.executable, str(ROOT / "server.py"), "0"],
                               env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               encoding="utf-8")
    line = process.stdout.readline()
    match = re.search(r"127\.0\.0\.1:(\d+)", line)
    if not match:
        process.terminate()
        process.wait(timeout=10)
        raise AssertionError("Server did not announce its listening address")
    return process, int(match[1])


def stop(process):
    process.terminate()
    process.wait(timeout=10)
    process.stdout.close()


def main():
    checks = []
    with tempfile.TemporaryDirectory(prefix="daguanyuan-http-") as folder:
        process, port = launch(folder)
        try:
            status, headers, html = request(port, "GET", "/math")
            assert status == 200 and b"/app/app.js" in html
            assert "Access-Control-Allow-Origin" not in headers
            checks.append("personal page and same-origin response")
            for path in ["/app/../user-data/learning.sqlite3", "/app/%2e%2e/server.py",
                         "/app/C:/Windows/win.ini", "/raw/rea/application-evidence.json"]:
                assert request(port, "GET", path)[0] == 404, path
            checks.append("private files and traversal cannot be served")
            assert request(port, "POST", "/api/local/paper", {"list": []})[0] == 400
            assert request(port, "PUT", "/api/local/settings", {"daily_goal": 15},
                           {"Origin": "https://example.com"})[0] == 403
            assert request(port, "PUT", "/api/local/settings", {"daily_goal": 15},
                           {"Content-Type": "text/plain"})[0] == 415
            assert request(port, "GET", "/api/local/questions?page=nope")[0] == 400
            checks.append("invalid inputs and cross-site writes fail explicitly")
            status, _, raw = request(port, "GET", "/api/local/questions?list=all&q=%231214")
            page = json.loads(raw)["data"]
            assert status == 200 and page["total"] == 1 and page["items"][0]["id"] == 1214
            checks.append("exact question identity search")
            captures = []
            for name in ("complete", "exam", "mock"):
                source = ROOT / "mirror/recovered/questions" / f"{name}.json"
                if not source.exists():
                    source = ROOT / "scrape" / f"{name}.json"
                captures.append(json.loads(source.read_text(encoding="utf-8"))["items"])
            expected_year = {q["id"] for items in captures for q in items if "2025" in " ".join(str(q.get(key) or "") for key in ("stem", "source", "original_source", "category_name", "category_full_path"))}
            status, _, raw = request(port, "GET", "/api/local/questions?list=all&q=2025&per_page=100")
            year_page = json.loads(raw)["data"]
            assert status == 200 and year_page["total"] >= len(expected_year) > 1
            assert expected_year <= {q["id"] for q in year_page["items"]}
            _, _, raw = request(port, "GET", "/api/local/questions?list=all&q=%232025")
            identity_page = json.loads(raw)["data"]
            assert identity_page["total"] == 1 and identity_page["items"][0]["id"] == 2025
            checks.append("year keyword search and explicit question ID stay distinct")
            expected_core = {q["id"] for items in captures for q in items if q.get("is_core") is True}
            _, _, raw = request(port, "GET", "/api/local/bootstrap")
            bootstrap = json.loads(raw)["data"]
            assert bootstrap["list_counts"]["core"] == len(expected_core)
            actual_core = set()
            for page_no in range(1, (len(expected_core) + 99) // 100 + 1):
                status, _, raw = request(port, "GET", f"/api/local/questions?list=core&per_page=100&page={page_no}")
                page = json.loads(raw)["data"]
                assert status == 200 and page["total"] == len(expected_core)
                assert all(q.get("is_core") is True for q in page["items"])
                actual_core.update(q["id"] for q in page["items"])
            assert actual_core == expected_core
            for node in bootstrap["directories"]["core"]:
                category = node.get("category_id", node.get("id"))
                _, _, raw = request(port, "GET", f"/api/local/questions?list=core&category={category}&per_page=1")
                assert json.loads(raw)["data"]["total"] == node["local_question_count"]
            checks.append("core flags, deduplication, all pages and directory subtree counts")
            assert request(port, "PUT", "/api/local/questions/3356/state",
                           {"favorite": True, "note": "HTTP persistence verification"})[0] == 200
            assert request(port, "POST", "/api/local/questions/3356/attempt",
                           {"result": "wrong", "answer": "verification", "duration_seconds": 3})[0] == 200
            _, _, raw = request(port, "GET", "/api/local/export")
            exported = json.loads(raw)["data"]
            assert exported["states"]["3356"]["favorite"] and len(exported["records"]) == 1
            for _ in range(2):
                status, _, raw = request(port, "POST", "/api/local/import", exported)
                assert status == 200
                backup = Path(json.loads(raw)["data"]["backup_path"])
                assert backup.is_file() and backup.is_relative_to(Path(folder))
            checks.append("JSON export, backup-before-import and idempotent merge")
        finally:
            stop(process)
        process, port = launch(folder)
        try:
            _, _, raw = request(port, "GET", "/api/local/bootstrap")
            stats = json.loads(raw)["data"]["stats"]
            assert stats["favorites"] == 1 and stats["wrong"] == 1 and stats["total_attempts"] == 1
            checks.append("learning data survives process restart")
        finally:
            stop(process)
    report = {"passed": len(checks), "checks": checks,
              "scope": "real Python HTTP server with original local question captures and isolated SQLite"}
    (ROOT / "raw" / "http-verification.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
