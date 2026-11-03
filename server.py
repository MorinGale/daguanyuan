#!/usr/bin/env python3
"""大观园本地复刻 - API 服务

从 mirror/api-data/ 与 scrape/ 的快照数据提供原站 API 形状的响应。
用法: python server.py [port] [--open]   (默认 3101)
"""
import json
import os
import re
import sys
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from local_app import LocalApp

ROOT = os.path.dirname(os.path.abspath(__file__))
MIRROR = os.path.join(ROOT, "mirror")
API_DATA = os.path.join(MIRROR, "api-data")
SCRAPE = os.path.join(ROOT, "scrape")
APP = os.path.join(ROOT, "app")
LOCAL = LocalApp()

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".avif": "image/avif",
    ".jpg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".ttf": "font/ttf",
    ".map": "application/json",
    ".txt": "text/plain; charset=utf-8",
    ".xml": "application/xml; charset=utf-8",
}

_questions_cache = {}

def load_questions(list_name):
    if list_name not in _questions_cache:
        path = os.path.join(SCRAPE, f"{list_name}.json")
        with open(path, encoding="utf-8") as f:
            _questions_cache[list_name] = json.load(f)["items"]
    return _questions_cache[list_name]

def load_fixture(name, fallback=None):
    path = os.path.join(API_DATA, name)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return fallback

def ok_data(data):
    return {"code": 0, "message": "OK", "data": data}

def find_category(cat_id, cats):
    """Depth-first search in nested category tree."""
    for c in cats:
        if c["id"] == cat_id:
            return c
        found = find_category(cat_id, c.get("children") or [])
        if found:
            return found
    return None

def collect_subtree_ids(cat, out):
    out.add(cat["id"])
    for ch in cat.get("children") or []:
        collect_subtree_ids(ch, out)

def page_items(items, params):
    page = max(1, int(params.get("page", ["1"])[0] or 1))
    per_page = min(200, max(1, int(params.get("per_page", ["30"])[0] or 30)))
    start = (page - 1) * per_page
    chunk = items[start:start + per_page]
    return {
        "items": chunk,
        "total": len(items),
        "page": page,
        "per_page": per_page,
        "total_pages": (len(items) + per_page - 1) // per_page,
    }

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.command, self.path))

    def _send(self, status, body, ctype="application/json; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store" if "/api/" in self.path else "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, status=200):
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _file(self, relpath):
        path = os.path.normpath(os.path.join(MIRROR, relpath.lstrip("/")))
        try:
            allowed = os.path.commonpath([MIRROR, path]) == MIRROR
        except ValueError:
            allowed = False
        if not allowed or not os.path.isfile(path):
            return False
        ext = os.path.splitext(path)[1].lower()
        with open(path, "rb") as f:
            self._send(200, f.read(), MIME.get(ext, "application/octet-stream"))
        return True

    def do_OPTIONS(self):
        self._json({"code": 1403, "message": "仅支持同源本地访问"}, 403)

    def _local_api(self, method, path, params):
        body = None
        if method != "GET":
            origin = self.headers.get("Origin")
            expected = "http://" + self.headers.get("Host", "")
            if origin and origin != expected:
                self.close_connection = True
                return self._json({"code": 1403, "message": "不接受跨站写入"}, 403)
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                self.close_connection = True
                return self._json({"code": 1415, "message": "请求必须使用 application/json"}, 415)
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 0 or length > 16 * 1024 * 1024:
                    self.close_connection = True
                    return self._json({"code": 1413, "message": "请求体超过 16 MB 处理预算"}, 413)
                body = json.loads(self.rfile.read(length))
            except (ValueError, UnicodeDecodeError):
                self.close_connection = True
                return self._json({"code": 1400, "message": "请求体不是有效 JSON"}, 400)
        status, result = LOCAL.dispatch(method, path, params, body)
        return self._json(result, status)

    def _app_file(self, path):
        from urllib.parse import unquote
        decoded = unquote(path)
        if decoded == "/raw/asset-recovery.json":
            base = os.path.join(ROOT, "raw")
            relative = "asset-recovery.json"
        elif decoded.startswith("/app/"):
            base = APP
            relative = decoded[len("/app/"):]
        else:
            return False
        filename = os.path.abspath(os.path.join(base, relative))
        try:
            allowed = os.path.commonpath([base, filename]) == base
        except ValueError:
            allowed = False
        if not allowed or not os.path.isfile(filename):
            return False
        with open(filename, "rb") as f:
            self._send(200, f.read(), MIME.get(os.path.splitext(filename)[1], "application/octet-stream"))
        return True

    # ---------------- API ----------------
    def _api(self, path, params):
        route = path.rstrip("/")

        # health / welcome
        if route == "/api/health":
            import datetime
            now = datetime.datetime.now().isoformat()
            return self._json({"status": "healthy", "timestamp": now})
        if route == "/api/health/live":
            return self._json({"status": "alive"})
        if route == "/api/health/ready":
            return self._json({"status": "ready"})
        if route == "/api/welcome":
            return self._json({"message": "Welcome to DaGuan Wall API", "version": "1.0"})

        # site configs
        if route == "/api/site/landing-config":
            return self._json(load_fixture("site-landing-config.json"))
        if route == "/api/site/math-home-config":
            return self._json(load_fixture("site-math-home-config.json"))
        if route == "/api/site/external-tools":
            return self._json(load_fixture("site-external-tools.json"))
        if route == "/api/site/maintenance":
            return self._json(load_fixture("site-maintenance.json"))

        # categories
        if route == "/api/categories":
            return self._json(load_fixture("categories-complete-stats.json"))

        # question lists
        m = re.match(r"^/api/question-lists/([^/]+)$", route)
        if m and m.group(1) in ("complete", "exam", "mock"):
            return self._json(load_fixture(f"ql-{m.group(1)}.json"))

        m = re.match(r"^/api/question-lists/([^/]+)/directory$", route)
        if m and m.group(1) in ("complete", "exam", "mock"):
            return self._json(load_fixture(f"dir-{m.group(1)}.json"))

        # questions listing (paged, filtered by category subtree)
        if route == "/api/questions":
            list_id = params.get("question_list_id", ["complete"])[0]
            cat_id = params.get("category_id", [None])[0]
            include_children = params.get("include_children", ["false"])[0] == "true"
            exam_subject = params.get("exam_subject", ["all"])[0]

            items = load_questions(list_id if list_id in ("complete", "exam", "mock") else "complete")
            if cat_id:
                try:
                    cat_id = int(cat_id)
                except ValueError:
                    cat_id = None
            if cat_id:
                cats = load_fixture("categories-complete-stats.json")
                node = find_category(cat_id, cats)
                if node and include_children:
                    ids = set()
                    collect_subtree_ids(node, ids)
                    items = [it for it in items if it.get("category_id") in ids or cat_id in (it.get("category_ids") or [])]
                elif node:
                    items = [it for it in items if it.get("category_id") == cat_id]
                else:
                    items = [it for it in items if it.get("category_id") == cat_id]
            if exam_subject and exam_subject != "all":
                items = [it for it in items if exam_subject in ((it.get("document") or {}).get("effective_exam_subjects") or [exam_subject])]
            return self._json(ok_data(page_items(items, params)))

        # single question
        m = re.match(r"^/api/questions/(\d+)$", route)
        if m:
            qid = int(m.group(1))
            for name in ("complete", "exam", "mock"):
                for it in load_questions(name):
                    if it["id"] == qid:
                        return self._json(ok_data(it))
            return self._json({"code": 1404, "message": "Not found", "detail": "Not found"}, 404)

        # suggestions (read-only)
        if route == "/api/suggestions":
            return self._json(load_fixture("suggestions.json"))

        # auth endpoints -> unauthenticated responses matching original shapes
        if route == "/api/auth/me" or route == "/api/auth/mobile/me/math-exam-subject":
            return self._json({"code": 1401, "message": "Not authenticated", "detail": "Not authenticated"}, 401)
        if route == "/api/auth/csrf":
            return self._json({"code": 1401, "message": "Not authenticated", "detail": "Not authenticated"}, 401)
        if route == "/api/auth/wechat-qr/sessions":
            return self._json({"code": 0, "message": "QR login session created", "data": {
                "session_id": "local-mirror-session",
                "poll_token": "local-mirror-token",
                "qr_image_url": "/wechat-qrcode.webp",
                "expires_at": "2030-01-01T00:00:00+08:00",
                "poll_interval_ms": 2000,
            }})
        if route == "/api/auth/login":
            return self._json({"code": 1422, "message": "请求参数错误", "detail": "validation_error",
                               "errors": ["body.verification_code: 本地镜像不支持登录"]}, 422)
        if route == "/api/auth/logout":
            return self._json({"code": 0, "message": "OK", "data": {"logout": True}})

        # notifications/polls/user endpoints -> 401 like original when logged out
        if route.startswith("/api/notifications") or route.startswith("/api/polls") or \
           route.startswith("/api/user/") or route.startswith("/api/retest") or \
           route.startswith("/api/practice") or route.startswith("/api/v1/user"):
            return self._json({"code": 1401, "message": "Could not validate credentials",
                               "detail": "Could not validate credentials"}, 401)

        # generic 404 in original envelope
        return self._json({"error": "Not found"}, 404)

    # ---------------- routing ----------------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if path.startswith("/api/local/"):
            return self._local_api("GET", path, params)
        if path.startswith("/app/") or path == "/raw/asset-recovery.json":
            if self._app_file(path):
                return
            return self._send(404, b"Not found", "text/plain")
        if path in ("/", "/tools"):
            page = "index.html" if path == "/" else "tools.html"
            with open(os.path.join(MIRROR, "pages", page), "rb") as f:
                body = f.read().replace(b"</body>", b'<script src="/app/landing-navigation.js"></script></body>')
                return self._send(200, body, "text/html; charset=utf-8")
        if path == "/settings" or path.startswith("/math"):
            with open(os.path.join(APP, "index.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")

        # Next.js RSC flight requests: serve captured payloads
        if "_rsc" in params:
            key = re.sub(r"[^A-Za-z0-9]", "_", path.strip("/")) + ".rsc"
            if key == "_.rsc" or key == ".rsc":
                key = "index.rsc"
            if path.startswith("/math/practice"):
                key = "math_practice_listId_complete_categoryId_331.rsc"
            rsc_path = os.path.join(MIRROR, "rsc", key)
            if os.path.isfile(rsc_path):
                with open(rsc_path, "rb") as f:
                    return self._send(200, f.read(), "text/x-component; charset=utf-8")
            # unknown route: return math shell RSC to keep router stable
            fallback = os.path.join(MIRROR, "rsc", "math.rsc")
            if os.path.isfile(fallback):
                with open(fallback, "rb") as f:
                    return self._send(200, f.read(), "text/x-component; charset=utf-8")

        if path.startswith("/api/"):
            return self._api(path, params)

        # page routing (mirrored HTML)
        page_map = {
            "/": "index.html",
            "/math": "math.html",
            "/math/questions": "math_questions.html",
            "/math/practice": "math_practice.html",
            "/math/favorites": "math_favorites.html",
            "/math/mastery": "math_mastery.html",
            "/math/knowledge": "math_knowledge.html",
            "/math/retest": "math_retest.html",
            "/math/paper": "math_paper.html",
            "/math/notes": "math_notes.html",
            "/math/records": "math_records.html",
            "/tools": "tools.html",
            "/suggestions": "suggestions.html",
            "/login": "login.html",
            "/admin": "admin.html",
            "/oauth": "oauth.html",
            "/examples": "examples.html",
            "/settings": "settings.html",
        }
        if path in page_map:
            body = open(os.path.join(MIRROR, "pages", page_map[path]), "rb").read()
            return self._send(200, body, "text/html; charset=utf-8")
        if path == "/politics/paradiyu-materials.html":
            body = open(os.path.join(MIRROR, "pages", "politics_paradiyu-materials.html"), "rb").read()
            return self._send(200, body, "text/html; charset=utf-8")

        # SPA fallback for unknown client-side routes (e.g. /math/practice?...)
        if "." not in os.path.basename(path):
            # learning routes use the math shell; practice uses its own SSR snapshot
            if path.startswith("/math/practice"):
                body = open(os.path.join(MIRROR, "pages", "math_practice.html"), "rb").read()
            elif path.startswith("/math") or path.startswith("/settings") or path == "/login":
                body = open(os.path.join(MIRROR, "pages", "math.html"), "rb").read()
            else:
                body = open(os.path.join(MIRROR, "pages", "index.html"), "rb").read()
            return self._send(200, body, "text/html; charset=utf-8")

        if self._file(path):
            return
        return self._send(404, b"Not found", "text/plain; charset=utf-8")

    def do_POST(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        if parsed.path.startswith("/api/local/"):
            return self._local_api("POST", parsed.path, params)
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)  # drain
        if parsed.path.startswith("/api/"):
            return self._api(parsed.path, params)
        return self._send(404, b"Not found", "text/plain; charset=utf-8")

    def do_PUT(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/local/"):
            return self._local_api("PUT", parsed.path, parse_qs(parsed.query))
        self._send_401()
    def do_PATCH(self):
        self._send_401()
    def do_DELETE(self):
        self._send_401()

    def _send_401(self):
        self._json({"code": 1401, "message": "Not authenticated", "detail": "Not authenticated"}, 401)


class LocalHTTPServer(ThreadingHTTPServer):
    # Windows permits competing listeners with SO_REUSEADDR; an older process
    # can then keep answering requests even after a new server starts.
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def main():
    import webbrowser
    args = [arg for arg in sys.argv[1:] if arg != "--open"]
    port = int(args[0]) if args else 3101
    server = LocalHTTPServer(("127.0.0.1", port), Handler)
    port = server.server_port
    print(f"大观园个人学习空间 http://127.0.0.1:{port}/  (Ctrl-C 退出)", flush=True)
    if "--open" in sys.argv:
        webbrowser.open(f"http://127.0.0.1:{port}/math")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
