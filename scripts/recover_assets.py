#!/usr/bin/env python3
"""Recover only explicitly referenced question images, without credentials.

This is an ordinary download provenance manifest, not REA Evidence. Original
question JSON and asset identifiers are never rewritten. Authenticated assets
are recorded as missing; this script never logs in or guesses identifiers.
"""
from __future__ import annotations

import argparse
import copy
import concurrent.futures
import datetime
import hashlib
import json
import pathlib
import re
import shutil
import threading
import urllib.error
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
ORIGIN = "https://cxyonly.fans"
ASSET_REF = re.compile(r"^asset://(?:sha256/)?([a-fA-F0-9]{64})$")
IMAGE_REF = re.compile(r"!\[[^\]]*\]\(\s*(?:<([^>]+)>|([^\s)]+))")
SAFE_TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif",
              "image/webp": "webp", "image/avif": "avif"}
MAX_ASSET_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024


def reference_identity(reference):
    if not isinstance(reference, str):
        return None
    match = ASSET_REF.fullmatch(reference)
    if match:
        digest = match.group(1).lower()
        return digest, f"{ORIGIN}/api/v1/question-assets/{digest}"
    parsed = urllib.parse.urlsplit(reference)
    if parsed.scheme != "https" or parsed.netloc != "cxyonly.fans":
        return None
    match = re.fullmatch(r"/api/v1/question-assets/([a-fA-F0-9]{64})", parsed.path)
    if match and not parsed.query and not parsed.fragment:
        return match.group(1).lower(), reference
    return None


def collect_references():
    assets = {}
    source_files = []

    def add(reference, source):
        identity = reference_identity(reference)
        if identity is None:
            return
        digest, url = identity
        record = assets.setdefault(digest, {"content_sha256": digest,
                                            "source_url": url, "references": [],
                                            "sources": []})
        if reference not in record["references"]:
            record["references"].append(reference)
        if source not in record["sources"]:
            record["sources"].append(source)

    def walk(value, source):
        if isinstance(value, dict):
            if "question_id" in value:
                source = {**source, "question_id": value["question_id"]}
            if isinstance(value.get("asset_refs"), list):
                for reference in value["asset_refs"]:
                    add(reference, source)
            for key, child in value.items():
                if key != "asset_refs":
                    walk(child, source)
        elif isinstance(value, list):
            for child in value:
                walk(child, source)
        elif isinstance(value, str):
            for match in IMAGE_REF.finditer(value):
                add(match.group(1) or match.group(2), source)

    paths = list((ROOT / "scrape").glob("*.json"))
    paths.extend((ROOT / "mirror/recovered/questions").glob("*.json"))
    knowledge = ROOT / "app/data/knowledge-content.json"
    if knowledge.exists():
        paths.append(knowledge)
    for path in sorted(paths):
        raw = path.read_bytes()
        source_files.append({"path": path.relative_to(ROOT).as_posix(),
                             "sha256": hashlib.sha256(raw).hexdigest()})
        walk(json.loads(raw), {"file": path.relative_to(ROOT).as_posix()})
    return assets, source_files


class PublicOnlyRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlsplit(newurl)
        if parsed.scheme != "https" or parsed.netloc != "cxyonly.fans":
            raise ValueError("Refusing redirect outside the selected public HTTPS origin")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def valid_image(data, content_type):
    if content_type == "image/png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if content_type == "image/jpeg":
        return data.startswith(b"\xff\xd8\xff")
    if content_type == "image/gif":
        return data[:6] in (b"GIF87a", b"GIF89a")
    if content_type == "image/webp":
        return data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    return len(data) >= 16 and data[4:8] == b"ftyp" and b"avif" in data[8:32]


def recover(assets, workers, timeout):
    previous_path = ROOT / "raw/asset-recovery.json"
    previous = json.loads(previous_path.read_text(encoding="utf-8")) if previous_path.exists() else {}
    # Newly collected references own URLs and capture identities. Preserve the
    # prior acquisition evidence separately, including browser-derived images.
    retained_fields = ("browser_dom_provenance", "recovery_history", "acquisition_method", "imported_at")
    assets = copy.deepcopy(assets)
    for digest, record in assets.items():
        prior = previous.get("assets", {}).get(digest, {})
        if prior.get("content_sha256") == digest:
            for field in retained_fields:
                if field in prior:
                    record[field] = copy.deepcopy(prior[field])
    output = ROOT / "mirror" / "question-assets"
    output.mkdir(parents=True, exist_ok=True)
    source_unavailable = threading.Event()
    unavailable_reason = "source_unavailable"
    budget_exhausted = threading.Event()
    budget_lock = threading.Lock()
    bytes_received = 0
    reserved_bytes = 0
    opener = urllib.request.build_opener(PublicOnlyRedirects())

    def one(record):
        nonlocal bytes_received, reserved_bytes, unavailable_reason
        digest = record["content_sha256"]
        for content_type, extension in SAFE_TYPES.items():
            path = output / f"{digest}.{extension}"
            if path.is_file() and path.stat().st_size <= MAX_ASSET_BYTES:
                data = path.read_bytes()
                if hashlib.sha256(data).hexdigest() == digest and valid_image(data, content_type):
                    return {**record, "status": "existing", "path": path.relative_to(ROOT / "mirror").as_posix(),
                            "sha256": digest, "bytes": len(data), "content_type": content_type}
        if source_unavailable.is_set():
            return {**record, "status": "deferred", "reason": unavailable_reason,
                    "attempted": False}
        if budget_exhausted.is_set():
            return {**record, "status": "deferred", "reason": "total_byte_budget_exceeded",
                    "attempted": False}
        request = urllib.request.Request(record["source_url"], headers={
            "User-Agent": "DaguanyuanPersonalMirror/1.0", "Accept": ", ".join(SAFE_TYPES)})
        try:
            with opener.open(request, timeout=timeout) as response:
                content_type = response.headers.get_content_type().lower()
                if content_type not in SAFE_TYPES:
                    raise ValueError(f"unsafe_or_unsupported_content_type:{content_type}")
                declared_size = response.headers.get("Content-Length")
                if declared_size is not None and int(declared_size) > MAX_ASSET_BYTES:
                    raise ValueError("asset_byte_budget_exceeded")
                chunks = []
                size = 0
                while True:
                    with budget_lock:
                        allowance = min(65536, MAX_ASSET_BYTES + 1 - size,
                                        MAX_TOTAL_BYTES - bytes_received - reserved_bytes)
                        if allowance <= 0:
                            budget_exhausted.set()
                            raise ValueError("total_byte_budget_exceeded")
                        reserved_bytes += allowance
                    try:
                        chunk = response.read(allowance)
                    except Exception:
                        with budget_lock:
                            reserved_bytes -= allowance
                        raise
                    with budget_lock:
                        reserved_bytes -= allowance
                        bytes_received += len(chunk)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_ASSET_BYTES:
                        raise ValueError("asset_byte_budget_exceeded")
                    chunks.append(chunk)
                data = b"".join(chunks)
                actual_digest = hashlib.sha256(data).hexdigest()
                if not valid_image(data, content_type):
                    raise ValueError("image_signature_mismatch")
                if actual_digest != digest:
                    return {**record, "status": "failed", "attempted": True,
                            "reason": "content_sha256_mismatch", "sha256": actual_digest,
                            "bytes": size, "content_type": content_type}
                path = output / f"{digest}.{SAFE_TYPES[content_type]}"
                temporary = path.with_suffix(path.suffix + ".part")
                temporary.write_bytes(data)
                temporary.replace(path)
                return {**record, "status": "downloaded", "attempted": True,
                        "path": path.relative_to(ROOT / "mirror").as_posix(),
                        "sha256": actual_digest, "bytes": size,
                        "content_type": content_type, "final_url": response.url}
        except urllib.error.HTTPError as error:
            if error.code in (401, 403, 429, 503):
                unavailable_reason = "authentication_required" if error.code in (401, 403) else f"source_http_{error.code}"
                source_unavailable.set()
            return {**record, "status": "failed", "attempted": True,
                    "http_status": error.code,
                    "reason": unavailable_reason if error.code in (401, 403, 429, 503) else f"http_{error.code}"}
        except Exception as error:
            return {**record, "status": "failed", "attempted": True,
                    "reason": f"{type(error).__name__}:{error}"}

    # Probe one exact known reference first to avoid hundreds of denied requests.
    records = list(assets.values())
    results = [one(records[0])] if records else []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        results.extend(executor.map(one, records[1:]))
    return {record["content_sha256"]: record for record in results}, bytes_received


def preserve_manifest_provenance(report, previous):
    """Keep the provenance label consistent with retained acquisition records."""
    if any(record.get("browser_dom_provenance") for record in report["assets"].values()):
        report["provenance_kind"] = "asset_recovery_manifest_not_rea_evidence"
        report["authentication"] = "public download attempts: none; rendered DOM images: existing user browser session"
        report["original_download_provenance"] = copy.deepcopy(previous.get("original_download_provenance", {
            "provenance_kind": previous.get("provenance_kind"), "authentication": previous.get("authentication")}))
        report["summary"]["browser_dom_available"] = sum(
            bool(record.get("browser_dom_provenance")) and record["status"] in ("existing", "downloaded")
            for record in report["assets"].values())
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=4, choices=range(1, 5))
    parser.add_argument("--timeout", type=float, default=20)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    assets, sources = collect_references()
    if args.dry_run:
        print(json.dumps({"referenced_assets": len(assets), "source_files": sources}, ensure_ascii=False, indent=2))
        return
    results, byte_count = recover(assets, args.workers, args.timeout)
    available = sum(r["status"] in ("downloaded", "existing") for r in results.values())
    report = {
        "format_version": 1,
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "provenance_kind": "public_asset_download_manifest_not_rea_evidence",
        "origin": ORIGIN,
        "authentication": "none; no login or credential use",
        "openapi_path": "/api/v1/question-assets/{content_sha256}",
        "openapi_security": "HTTPBearer declared; public access is tested without authentication",
        "limits": {"workers": args.workers, "timeout_seconds": args.timeout,
                   "max_asset_bytes": MAX_ASSET_BYTES, "max_total_bytes": MAX_TOTAL_BYTES},
        "summary": {"referenced": len(results), "available": available,
                    "missing": len(results) - available, "bytes_received": byte_count,
                    "attempted": sum(r.get("attempted", False) for r in results.values())},
        "source_files": sources,
        "assets": results,
    }
    path = ROOT / "raw" / "asset-recovery.json"
    if path.exists():
        previous = json.loads(path.read_text(encoding="utf-8"))
        preserve_manifest_provenance(report, previous)
        backup = ROOT / "backups" / ("asset-before-public-recovery-" + datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S-%f"))
        backup.mkdir(parents=True)
        shutil.copy2(path, backup / path.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.part")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    print(json.dumps(report["summary"], ensure_ascii=False))


if __name__ == "__main__":
    main()
