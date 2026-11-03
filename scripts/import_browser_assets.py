#!/usr/bin/env python3
"""Import exact, referenced images saved from the original site's rendered DOM.

This command reads local files only. It never opens a browser, sends requests,
or reads session credentials. The caller supplies the page actually observed.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil

try:
    from .recover_assets import MAX_ASSET_BYTES, SAFE_TYPES, valid_image
except ImportError:
    from recover_assets import MAX_ASSET_BYTES, SAFE_TYPES, valid_image

ROOT = Path(__file__).resolve().parents[1]
PAGE = re.compile(r"https://cxyonly\.fans/math/questions/([1-9]\d*)/?")
FILE = re.compile(r"([a-f0-9]{64})\.(png|jpe?g|webp|gif|avif)")
MIME = {extension: content_type for content_type, extension in SAFE_TYPES.items()}
MIME["jpeg"] = "image/jpeg"


def import_assets(root, source_url=None, filenames=None, observations=None):
    root = Path(root).resolve()
    incoming = root / "raw/question-image-dom"
    manifest_path = root / "raw/asset-recovery.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    previous = copy.deepcopy(manifest)
    if observations is not None:
        observation_path = incoming / observations
        if observation_path.parent.resolve() != incoming.resolve() or observation_path.is_symlink():
            raise ValueError("Observations must be a direct file in raw/question-image-dom")
        saved = json.loads(observation_path.read_text(encoding="utf-8")).get("saved")
        if not isinstance(saved, dict):
            raise ValueError("Observations must contain a saved image map")
        candidates = []
        for digest, observed in saved.items():
            if not isinstance(observed, dict) or not re.fullmatch(r"[a-f0-9]{64}", digest) or observed.get("sha256") != digest:
                raise ValueError("Observation SHA256 identity is invalid")
            files = [path for path in incoming.glob(digest + ".*") if FILE.fullmatch(path.name)]
            if len(files) != 1:
                raise ValueError(f"Expected exactly one captured image for {digest}")
            candidates.append((files[0], observed.get("source_url"), observed))
    else:
        paths = [incoming / name for name in dict.fromkeys(filenames)] if filenames else sorted(incoming.iterdir())
        candidates = [(path, source_url, None) for path in paths if path.suffix != ".json"]
    validated = []
    for path, observed_url, observed in candidates:
        page = PAGE.fullmatch(observed_url) if isinstance(observed_url, str) else None
        if not page:
            raise ValueError("source-url must be the observed original question page")
        question_id = int(page[1])
        if observed is not None and (type(observed.get("question_id")) is not int or observed["question_id"] != question_id):
            raise ValueError("Observation question identity does not match its source URL")
        if path.parent.resolve() != incoming.resolve() or path.is_symlink():
            raise ValueError("Only direct files from raw/question-image-dom may be imported")
        match = FILE.fullmatch(path.name)
        if not match or not path.is_file() or path.stat().st_size > MAX_ASSET_BYTES:
            raise ValueError(f"Invalid image file: {path.name}")
        digest, extension = match.groups()
        record = manifest["assets"].get(digest)
        if not record or record.get("content_sha256") != digest:
            raise ValueError(f"Image is not an existing captured reference: {path.name}")
        if not any(source.get("question_id") == question_id for source in record.get("sources", [])):
            raise ValueError(f"Image is not referenced by the supplied question: {path.name}")
        data = path.read_bytes()
        content_type = MIME[extension]
        if hashlib.sha256(data).hexdigest() != digest or not valid_image(data, content_type):
            raise ValueError(f"Image digest or MIME signature does not match: {path.name}")
        if observed is not None and observed.get("bytes") != len(data):
            raise ValueError(f"Observation byte count does not match: {path.name}")
        relative = f"question-assets/{digest}.{SAFE_TYPES[content_type]}"
        target = root / "mirror" / relative
        if target.exists() and target.read_bytes() != data:
            raise ValueError(f"Existing asset has conflicting bytes: {relative}")
        provenance = {"kind": "browser_rendered_dom_image", "source_url": observed_url,
                      "question_id": question_id, "captured_file": path.relative_to(root).as_posix(),
                      "sha256": digest, "bytes": len(data), "content_type": content_type}
        if observed is not None:
            provenance["observation_file"] = observation_path.relative_to(root).as_posix()
            provenance["observation"] = copy.deepcopy(observed)
        already_recorded = provenance in record.get("browser_dom_provenance", [])
        if not already_recorded or record.get("path") != relative or record.get("status") not in ("existing", "downloaded") or not target.exists():
            validated.append((record, data, target, relative, provenance))

    if not validated:
        return {"imported": 0, "available": previous["summary"]["available"], "changed": False}

    stamp = datetime.now(timezone.utc)
    backup = root / "backups" / ("asset-before-browser-import-" + stamp.strftime("%Y%m%d-%H%M%S-%f"))
    backup.mkdir(parents=True)
    shutil.copy2(manifest_path, backup / manifest_path.name)
    for record, data, target, relative, provenance in validated:
        if not record.get("browser_dom_provenance"):
            record.setdefault("recovery_history", []).append({
                "manifest_generated_at": previous.get("generated_at"),
                **{key: record[key] for key in ("source_url", "status", "attempted", "reason", "http_status") if key in record},
            })
        record.setdefault("browser_dom_provenance", [])
        if provenance not in record["browser_dom_provenance"]:
            record["browser_dom_provenance"].append(provenance)
        record.update(status="existing", path=relative, sha256=provenance["sha256"],
                      bytes=len(data), content_type=provenance["content_type"],
                      acquisition_method="browser_rendered_dom", imported_at=stamp.isoformat())
        record.pop("reason", None)
        record.pop("http_status", None)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            temporary = target.with_suffix(target.suffix + ".part")
            temporary.write_bytes(data)
            temporary.replace(target)

    available = sum(bool(r.get("path")) and r.get("status") in ("existing", "downloaded")
                    for r in manifest["assets"].values())
    manifest["summary"].update(available=available, missing=len(manifest["assets"]) - available,
                               browser_dom_available=sum(bool(r.get("browser_dom_provenance")) for r in manifest["assets"].values()))
    manifest.setdefault("original_download_provenance", {
        "provenance_kind": previous.get("provenance_kind"), "authentication": previous.get("authentication")})
    manifest["provenance_kind"] = "asset_recovery_manifest_not_rea_evidence"
    manifest["authentication"] = "public download attempts: none; rendered DOM images: existing user browser session"
    manifest["last_updated_at"] = stamp.isoformat()
    temporary = manifest_path.with_suffix(".json.part")
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(manifest_path)
    return {"imported": len(validated), "available": available, "changed": True,
            "backup_path": str(backup)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--source-url", help="Original question page observed by the caller")
    source.add_argument("--observations", nargs="?", const="observations.json", help="Import only the saved entries in a local observation manifest")
    parser.add_argument("--file", action="append", dest="filenames", help="One captured image filename; repeat as needed")
    args = parser.parse_args()
    try:
        if args.observations and args.filenames:
            parser.error("--file cannot be combined with --observations")
        result = import_assets(ROOT, args.source_url, args.filenames, args.observations)
    except (ValueError, OSError) as error:
        parser.exit(1, f"{error}\n")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
