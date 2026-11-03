"""Combine retained UI provenance with current, separately scoped content reports."""
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import re

ROOT = Path(__file__).resolve().parents[1]


def read(relative, default=None):
    path = ROOT / relative
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def source(relative):
    path = ROOT / relative
    return {"path": relative, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()} if path.exists() else {"path": relative, "status": "not_saved"}


def main():
    previous = read("raw/ui-reference/restoration.json", {})
    styles = re.findall(r'href="(/_next/[^"]+\.css)"', (ROOT / "app/index.html").read_text(encoding="utf-8"))
    knowledge = read("raw/knowledge-dom/coverage.json", {})
    assets = read("raw/asset-recovery.json", {})
    questions = read("raw/question-coverage.json", {})
    http = read("raw/http-verification.json", {})
    current_checks = read("raw/content-restoration-validation.json", {})
    catalog = read("app/data/knowledge.json", {"catalog": []})["catalog"]
    available_image_ids = sorted({item["question_id"] for record in assets.get("assets", {}).values()
                                  if record.get("status") in {"existing", "downloaded"} and record.get("path")
                                  for item in record.get("browser_dom_provenance", []) if "question_id" in item})
    report = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "reference_origin": "https://cxyonly.fans/",
        "objective": "Restore original palette, fonts and UI for one local user. Content recovery remains partial.",
        "evidence": previous.get("evidence", {"static_rea": "ev_13a846749d4e1cf505276f2de5ebff071231689ae8567e30eb72fd95eea7af52",
                                               "parsed_javascript_files": 94, "parse_failures": 0, "semantic_coverage": "partial"}),
        "original_stylesheets": [source("mirror" + path) for path in styles],
        "core": {"scope": "core", "source_flag": "is_core",
                 "captured_unique_count": questions.get("core", {}).get("unique_question_count"),
                 "provenance": "raw/rea/core-restoration.json", "coverage": "retained captures only; current upstream total not verified"},
        "knowledge": {"catalog": "app/data/knowledge.json", "subjects": len(catalog),
                      "chapters": sum(len(tree["chapters"]) for tree in catalog),
                      "points": sum(len(chapter["items"]) for tree in catalog for chapter in tree["chapters"]),
                      "catalog_provenance": "raw/ui-reference/knowledge-extraction.json",
                      "content_report": source("raw/knowledge-dom/coverage.json"),
                      "published_content": source("app/data/knowledge-content.json"),
                      "content_origin": "Retained original-site rendered DOM converted to Markdown using original TeX annotations; not original Markdown source or REA Evidence.",
                      "counts": {key: knowledge.get(key) for key in ("saved", "failed", "unattempted", "validated_dom_capture_count", "validated_tex_annotations")},
                      "limitation": "Unattempted does not mean upstream content does not exist."},
        "images": {"report": source("raw/asset-recovery.json"), "counts": assets.get("summary", {}),
                   "browser_capture_source": source("raw/question-image-dom/observations.json"),
                   "available_browser_question_ids": available_image_ids,
                   "validation": "Importer checks SHA256, image format and original question identity before local publication."},
        "questions": {"report": source("raw/question-coverage.json"),
                      "unique_count": questions.get("union", {}).get("unique_question_count"),
                      "lists": {name: {key: data.get(key) for key in ("unique_question_count", "active_source", "saved_directory_total", "definition_item_count_delta", "directory_total_delta")}
                                | {"official_item_count": data.get("saved_definition", {}).get("item_count"),
                                   "root_counts": [{key: node.get(key) for key in ("category_id", "saved_question_count", "capture_with_cross_list_category_evidence_count")}
                                                   for node in data.get("directory_nodes", []) if node["depth"] == 0]}
                                for name, data in questions.get("lists", {}).items()},
                      "local_capture_search": source("raw/question-local-capture-search.json"),
                      "upstream_total_observation": {"visible_value": 7072,
                                                     "source": "Read-only original-site browser observation during this session",
                                                     "scope": "Not established to match retained official-list or deduplicated-local scope; not a verified missing-ID count."},
                      "limitation": "No new question IDs were recovered. Count deltas do not identify missing IDs or establish current full upstream totals."},
        "search": {"behavior": "Bare 2025 searches year/source and text; #2025 selects exact identity while preserving list filters.",
                   "retained_all_scope_2025_count": 67, "http_evidence": source("raw/http-verification.json")},
        "previous_restoration_validation": previous.get("previous_restoration_validation", previous.get("validation", {})),
        "current_content_validation": {"record": source("raw/content-restoration-validation.json"),
                                       "checks": current_checks, "http_record": source("raw/http-verification.json"),
                                       "http_checks": http.get("passed"), "http_scope": http.get("scope"),
                                       "browser": {"source": "Current local browser acceptance reported during this session",
                                                   "verified": ["C01.01: three section headings, paragraphs, original formulas and safe reference links visible", "question 1643: complete image natural size 369x318 and no document horizontal overflow", "all-list/all-subject search 2025 returns 67; #2025 returns one exact question"],
                                                   "knowledge_lists": {"point": "C06.04", "lists": 1, "items": 2, "formula_errors": 0, "horizontal_overflow": False},
                                                   "screenshots": ["raw/screenshots/restored-knowledge-content.png", "raw/screenshots/restored-question-image.png", "raw/screenshots/restored-year-search.png", "raw/screenshots/restored-knowledge-lists.png"]}},
        "screenshots": list(dict.fromkeys(previous.get("screenshots", []) + ["restored-knowledge-content.png", "restored-question-image.png", "restored-year-search.png", "restored-knowledge-lists.png"])),
        "limitations": [
            "Image and knowledge-prose recovery remain partial; exact current coverage comes from linked reports.",
            "Repeated upstream knowledge/detail failures were observed, including L01.01, P01.01 and C09.01; broad capture stopped. This does not prove absent upstream content.",
            "Public unauthenticated synchronization encountered 401/503 and stopped; retained content was preserved.",
            "Exam and mock saved official counts disagree with retained captures; full original-list completeness is not established.",
            "Local retest cadence is next day; original scheduling unknown.",
            "AI, video, rich notes and mastery-card generation unimplemented; multiplayer intentionally omitted.",
            "Not a pixel-perfect or private-backend reconstruction.",
            "Mirrored Next.js pages previously emitted recoverable React hydration #418 logs. Historical landing/tools checks remain separately retained.",
        ],
        "supersedes_ui_report": "raw/frontend-final.json",
        "notes": ["Original module stylesheets, palette, font stack and UI layout retained.",
                  "Original captures and formal learning data preserved; tests use isolated data.",
                  "Original service on port 3100 left untouched.",
                  "Historical UI validation remains historical and is separate from current content acceptance."],
    }
    path = ROOT / "raw/ui-reference/restoration.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Saved current content coverage and separately retained prior UI verification.")


if __name__ == "__main__":
    main()
