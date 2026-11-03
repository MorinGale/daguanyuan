"""Validate retained knowledge DOM captures and atomically publish local content.

No network or browser operations. Content is converted rendered DOM, not the
upstream Markdown source or a REA Evidence record. Original captures stay intact.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from urllib.parse import parse_qs, urlsplit


class CaptureError(ValueError):
    pass


class TexAnnotations(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.annotations = []
        self.parts = None

    def handle_starttag(self, tag, attrs):
        if tag == "annotation" and dict(attrs).get("encoding") == "application/x-tex":
            self.parts = []

    def handle_data(self, data):
        if self.parts is not None:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == "annotation" and self.parts is not None:
            self.annotations.append("".join(self.parts))
            self.parts = None


def read_json(path):
    raw = path.read_bytes()
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".part", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def markdown_formulas(markdown):
    # The retained converter adds newlines inside display delimiters. Only that
    # outer padding is ignored; TeX commands/internal whitespace stay untouched.
    pattern = r"(?<!\\)\$\$(.*?)\$\$|(?<![\\$])\$(?!\$)(.*?)(?<!\\)\$(?!\$)"
    return [display[1:-1] if display.startswith("\n") and display.endswith("\n") else display
            if display else inline for display, inline in re.findall(pattern, markdown, re.S)]


def validate_capture(capture, point, chapter, filename_code):
    code = point["conceptCode"]
    if filename_code != code or capture.get("conceptCode") != code or capture.get("reviewId") != point["reviewId"]:
        raise CaptureError("catalog_code_or_review_id_mismatch")
    if capture.get("name") != point["name"]:
        raise CaptureError("catalog_name_mismatch")
    url = urlsplit(capture.get("url", ""))
    query = parse_qs(url.query)
    if (url.scheme != "https" or url.netloc != "cxyonly.fans" or url.path != "/math/knowledge"
            or query.get("chapter") != [chapter] or query.get("point") != [point["reviewId"]]
            or query.get("view") != ["explanation"]):
        raise CaptureError("catalog_url_identity_mismatch")
    markdown = capture.get("content_markdown")
    html = capture.get("html")
    if not isinstance(markdown, str) or not markdown.strip() or not isinstance(html, str) or not html.strip():
        raise CaptureError("empty_markdown_or_dom")
    if capture.get("provenance_kind") != "rendered_dom_to_markdown_with_original_tex_annotations":
        raise CaptureError("unknown_conversion_provenance")
    parser = TexAnnotations()
    parser.feed(html)
    if parser.parts is not None:
        raise CaptureError("unterminated_tex_annotation")
    original = parser.annotations
    formulas = markdown_formulas(markdown)
    if original != formulas:
        raise CaptureError("tex_annotation_sequence_mismatch")
    if type(capture.get("formula_count")) is not int or capture["formula_count"] != len(original):
        raise CaptureError("reported_formula_count_mismatch")
    return {"annotation_count": len(original), "markdown_formula_count": len(formulas),
            "sequence_matches": True, "comparison": "exact TeX sequence after removing generated display newline framing only",
            "annotation_sequence_sha256": hashlib.sha256(json.dumps(parser.annotations, ensure_ascii=False).encode()).hexdigest()}


def publish(root):
    root = Path(root).resolve()
    directory = root / "raw/knowledge-dom"
    catalog, catalog_sha = read_json(root / "app/data/knowledge.json")
    points = {}
    by_review_id = {}
    for tree in catalog["catalog"]:
        for chapter in tree["chapters"]:
            for point in chapter["items"]:
                code = point["conceptCode"]
                if code in points or point["reviewId"] in by_review_id:
                    raise CaptureError("duplicate_catalog_identity")
                points[code] = (point, chapter["reviewId"])
                by_review_id[point["reviewId"]] = code
    destination = root / "app/data/knowledge-content.json"
    prior = {"schema_version": 1, "contents": {}}
    if destination.exists():
        prior, _ = read_json(destination)
        if prior.get("schema_version") != 1 or not isinstance(prior.get("contents"), dict):
            raise CaptureError("invalid_existing_content_file")
    contents = dict(prior["contents"])
    validated, rejected, attempts, sources = [], [], set(), []
    for path in sorted(directory.glob("pgmath.*.json")):
        filename_code = path.stem
        if filename_code in points:
            attempts.add(filename_code)
        try:
            capture, sha = read_json(path)
            if filename_code not in points:
                raise CaptureError("unknown_catalog_code")
            point, chapter = points[filename_code]
            validation = validate_capture(capture, point, chapter, filename_code)
            relative = path.relative_to(root).as_posix()
            entry = {"code": filename_code, "name": point["name"],
                     "content_markdown": capture["content_markdown"],
                     "source_url": capture["url"], "source_capture": relative,
                     "source_sha256": sha, "captured_at": capture.get("extracted_at"),
                     "source_kind": capture["provenance_kind"], "tex_validation": validation}
            contents[filename_code] = entry
            validated.append({"code": filename_code, "source": relative, "sha256": sha,
                              "annotation_count": validation["annotation_count"]})
        except (ValueError, TypeError, KeyError) as exc:
            rejected.append({"source": path.relative_to(root).as_posix(), "code": filename_code,
                             "reason": str(exc)})
    failure_observations = []
    for filename in ("deferred.json", "failures.json"):
        path = directory / filename
        if not path.exists():
            continue
        records, sha = read_json(path)
        sources.append({"path": path.relative_to(root).as_posix(), "sha256": sha,
                        "role": "failure observations, not missing-content evidence"})
        for record in records:
            review_id = record.get("reviewId")
            query = parse_qs(urlsplit(record.get("url") or record.get("link", {}).get("url", "")).query)
            if not review_id:
                review_id = (query.get("point") or [None])[0]
            code = by_review_id.get(review_id)
            if code:
                attempts.add(code)
            failure_observations.append({"code": code, "source": filename, "observation": record})
    links = directory / "links.json"
    if links.exists():
        link_data, sha = read_json(links)
        sources.append({"path": links.relative_to(root).as_posix(), "sha256": sha,
                        "role": "rendered original-site chapter/point directory links",
                        "chapter_links": len(link_data.get("chapters", [])),
                        "point_links": len(link_data.get("points", []))})
    saved = {code for code, (point, _) in points.items()
             if isinstance(contents.get(code), dict) and contents[code].get("code") == code
             and contents[code].get("name") == point["name"]
             and isinstance(contents[code].get("content_markdown"), str) and contents[code]["content_markdown"].strip()}
    failed = attempts - saved
    unattempted = set(points) - saved - attempts
    output = {**prior, "schema_version": 1, "contents": dict(sorted(contents.items()))}
    changed = output != prior
    backup = None
    if changed:
        if destination.exists():
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            backup_path = root / "backups/knowledge-dom" / stamp / destination.name
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(destination, backup_path)
            backup = backup_path.relative_to(root).as_posix()
        atomic_json(destination, output)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "method": "Validated conversion of retained rendered DOM to Markdown, preserving original TeX annotations; not original upstream Markdown and not a REA Evidence record.",
        "catalog_source": {"path": "app/data/knowledge.json", "sha256": catalog_sha},
        "catalog_total": len(points), "expected_catalog_total": 387, "catalog_matches_expected_total": len(points) == 387,
        "saved": len(saved), "failed": len(failed), "unattempted": len(unattempted),
        "validated_dom_capture_count": len(validated),
        "validated_tex_annotations": sum(item["annotation_count"] for item in validated),
        "saved_codes": sorted(saved), "failed_codes": sorted(failed), "unattempted_codes": sorted(unattempted),
        "validated_captures": validated, "rejected_captures": rejected,
        "failure_observations": failure_observations, "directory_and_failure_sources": sources,
        "published_path": "app/data/knowledge-content.json", "changed": changed, "backup": backup,
        "limitations": ["The snapshot covers only retained successful DOM captures and preserved local entries.",
                       "A prior loading failure can be resolved by a later successful capture; saved points take precedence over failure history.",
                       "TeX validation checks count and ordered annotation content, removing generated display newline framing only; it does not establish mathematical correctness."]}
    atomic_json(directory / "coverage.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    report = publish(args.root)
    print(json.dumps({key: report[key] for key in ("catalog_total", "saved", "failed", "unattempted", "validated_tex_annotations", "changed")}, ensure_ascii=True))


if __name__ == "__main__":
    main()
