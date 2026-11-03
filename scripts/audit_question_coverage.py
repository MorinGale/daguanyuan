"""Audit retained question captures without network access or source mutation.

Run: python scripts/audit_question_coverage.py
Counts are snapshot observations, not a claim about the current live service.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

CAPTURES = ("complete", "exam", "mock")
DISCIPLINES = {223: "advanced_math", 1: "linear_algebra", 601: "probability"}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def present(value):
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return any(present(v) for v in value.values())
    if isinstance(value, list):
        return any(present(v) for v in value)
    return value is not None


def category_ids(question):
    values = question.get("category_ids") or [question.get("category_id")]
    return {int(v) for v in values if v is not None}


def inspect_local_captures(root, known_ids):
    """Locate structurally recognizable question records in retained captures.

    This is an inventory, not an authorization to manufacture list membership or
    publish a validated snapshot from unrelated individual questions.
    """
    record_sources, parse_failures, rendered_pages = [], [], []
    checked, raw_candidates = 0, []
    excluded = {"question-coverage.json", "question-local-capture-search.json"}

    def visit(value, found):
        if isinstance(value, dict):
            qid = value.get("id", value.get("question_id"))
            document = value.get("document")
            if type(qid) is int and (
                    isinstance(value.get("stem"), str) or isinstance(value.get("stem_md"), str)
                    or isinstance(document, dict) and isinstance(document.get("stem_md"), str)):
                found.add(qid)
            for child in value.values():
                visit(child, found)
        elif isinstance(value, list):
            for child in value:
                visit(child, found)

    for folder in ("raw", "mirror", "scrape", "backups"):
        for path in sorted((root / folder).rglob("*")):
            if not path.is_file() or path.name in excluded or path.suffix not in {".json", ".jsonl", ".har", ".html", ".rsc"}:
                continue
            checked += 1
            raw = path.read_bytes()
            relative = path.relative_to(root).as_posix()
            if path.suffix in {".html", ".rsc"}:
                text = raw.decode("utf-8", errors="replace")
                links = {int(qid) for qid in re.findall(r'(?:/math/questions/|data-question-id=["\x27])(\d+)', text)}
                if "practice" in path.name or "questions" in path.name or links:
                    rendered_pages.append({"path": relative, "sha256": hashlib.sha256(raw).hexdigest(),
                                           "question_link_ids": sorted(links),
                                           "question_links_outside_active_captures": sorted(links - known_ids),
                                           "has_serialized_question_field_names": any(marker in raw for marker in (b"stem_md", b"correct_answer", b"answer_explanation"))})
                continue
            # A structurally recognizable question requires a stem field.
            if b"stem" not in raw:
                if path.name.endswith("manifest.json") and raw.lstrip().startswith(b"<"):
                    parse_failures.append({"path": relative, "reason": "HTML saved with JSON extension; not a data manifest",
                                           "sha256": hashlib.sha256(raw).hexdigest()})
                continue
            try:
                values = [json.loads(line) for line in raw.splitlines() if line.strip()] if path.suffix == ".jsonl" else [json.loads(raw)]
            except (ValueError, UnicodeError) as exc:
                parse_failures.append({"path": relative, "reason": type(exc).__name__,
                                       "sha256": hashlib.sha256(raw).hexdigest()})
                continue
            found = set()
            for value in values:
                visit(value, found)
            if found:
                new_ids = found - known_ids
                raw_candidates.extend(new_ids)
                record_sources.append({"path": relative, "sha256": hashlib.sha256(raw).hexdigest(),
                                       "recognized_unique_question_ids": len(found),
                                       "question_ids_outside_active_captures": sorted(new_ids)})
    return {
        "method": "Offline structural JSON/HAR/JSONL traversal and question-link inspection of retained HTML/RSC; no browser or network access.",
        "folders": ["raw", "mirror", "scrape", "backups"],
        "files_examined": checked, "record_sources": record_sources,
        "new_question_candidate_ids": sorted(set(raw_candidates)),
        "rendered_question_pages": rendered_pages, "unusable_json_files": parse_failures,
        "published_recovered_snapshots": [],
        "limitations": [
            "Finding an individual question does not establish its official exam/mock list membership or pagination completeness.",
            "HTML question links without serialized documents do not recover question/answer content.",
            "The scan does not deobfuscate JavaScript, decrypt archives, or infer missing IDs from gaps in numeric sequences.",
        ],
    }


def audit(root, scan_local=False):
    sources = []

    def load(relative):
        path = root / relative
        raw = path.read_bytes()
        sources.append({"path": relative, "sha256": hashlib.sha256(raw).hexdigest(),
                        "bytes": len(raw)})
        return json.loads(raw)

    baselines = {name: load(f"scrape/{name}.json") for name in CAPTURES}
    baseline_directories = {name: load(f"mirror/api-data/dir-{name}.json")["data"]
                   for name in CAPTURES}
    baseline_definitions = {name: load(f"mirror/api-data/ql-{name}.json")["data"]
                   for name in CAPTURES}
    captures, directories, definitions = dict(baselines), dict(baseline_directories), dict(baseline_definitions)
    selection = {}
    for name in CAPTURES:
        relative = f"mirror/recovered/questions/{name}.json"
        selected = {"path": f"scrape/{name}.json", "kind": "baseline", "validated_complete": False,
                    "definition_path": f"mirror/api-data/ql-{name}.json",
                    "directory_path": f"mirror/api-data/dir-{name}.json"}
        if (root / relative).exists():
            snapshot = load(relative)
            if snapshot.get("validated_complete") is True:
                items = snapshot.get("items")
                if (snapshot.get("list_id") != name or not isinstance(items, list)
                        or snapshot.get("fetched") != len(items)
                        or snapshot.get("api_total") != snapshot.get("fetched")
                        or not isinstance(snapshot.get("metadata"), dict)
                        or not isinstance(snapshot.get("directory"), dict)
                        or not isinstance(snapshot["directory"].get("roots"), list)):
                    raise ValueError(f"Invalid validated recovered snapshot: {relative}")
                captures[name] = snapshot
                directories[name] = snapshot["directory"]
                definitions[name] = snapshot["metadata"]
                selected = {"path": relative, "kind": "recovered", "validated_complete": True,
                            "definition_path": relative + "#/metadata",
                            "directory_path": relative + "#/directory"}
            else:
                selected["ignored_unvalidated_snapshot"] = relative
        baseline_ids = {q["id"] for q in baselines[name]["items"]}
        active_ids = {q["id"] for q in captures[name]["items"]}
        selection[name] = {
            "baseline": {"path": f"scrape/{name}.json", "unique_question_count": len(baseline_ids),
                         "fetched_at": baselines[name].get("fetched_at"),
                         "definition_path": f"mirror/api-data/ql-{name}.json",
                         "definition_item_count": baseline_definitions[name].get("item_count"),
                         "directory_path": f"mirror/api-data/dir-{name}.json",
                         "directory_total": baseline_directories[name].get("total")},
            "active_source": selected,
            "added_ids_since_baseline": sorted(active_ids - baseline_ids),
            "removed_ids_since_baseline": sorted(baseline_ids - active_ids),
        }
    stats = load("mirror/api-data/categories-complete-stats.json")
    occurrences = defaultdict(list)
    merged_categories = defaultdict(set)
    own_categories = {}
    list_ids = {}
    descendants = {}
    subjects = defaultdict(set)

    def index(nodes, inherited=()):
        result = set()
        for node in nodes:
            cid = int(node.get("category_id", node.get("id")))
            effective = node.get("effective_exam_subjects")
            if effective is None:
                effective = node.get("exam_subjects")
            if effective is None:
                effective = inherited
            if isinstance(effective, str):
                effective = [effective]
            subjects[cid].update(effective)
            children = index(node.get("children", []), effective)
            descendants.setdefault(cid, set()).update({cid} | children)
            result.update({cid} | children)
        return result

    index(stats)
    for data in directories.values():
        index(data["roots"])
    for name, capture in captures.items():
        list_ids[name] = {q["id"] for q in capture["items"]}
        own_categories[name] = defaultdict(set)
        for q in capture["items"]:
            qid = q["id"]
            occurrences[qid].append((name, q))
            merged_categories[qid].update(category_ids(q))
            own_categories[name][qid].update(category_ids(q))

    def matching(cids, pool, categories):
        return {qid for qid in pool if categories[qid] & cids}

    def dimensions(ids):
        types, disciplines, exams, flags = Counter(), Counter(), Counter(), Counter()
        for qid in ids:
            q = occurrences[qid][0][1]
            types[q.get("question_type") or q.get("document", {}).get("question_type") or "unknown"] += 1
            found_disciplines = [label for cid, label in DISCIPLINES.items()
                                 if merged_categories[qid] & descendants.get(cid, {cid})]
            for label in found_disciplines or ["unclassified_external_categories"]:
                disciplines[label] += 1
            applicable = set().union(*(subjects.get(cid, set()) for cid in merged_categories[qid]))
            for subject in applicable or {"unknown"}:
                exams[subject] += 1
            flags[str(q.get("is_core", "missing")).lower()] += 1
        return {"question_type": dict(types), "knowledge_discipline_membership": dict(disciplines),
                "exam_subject_membership": dict(exams), "canonical_is_core": dict(flags)}

    lists = {}
    for name, capture in captures.items():
        ids = list_ids[name]
        rows = []

        def visit(nodes, parents=()):
            for node in nodes:
                cid = int(node.get("category_id", node.get("id")))
                path = (*parents, node.get("label", node.get("name", "")))
                categories = descendants[cid]
                own = matching(categories, ids, own_categories[name])
                merged = matching(categories, ids, merged_categories)
                available = matching(categories, occurrences, merged_categories)
                expected = node.get("question_count")
                delta = len(merged) - expected if isinstance(expected, int) else None
                rows.append({"category_id": cid, "path": list(path), "depth": len(parents),
                             "saved_question_count": expected,
                             "capture_own_category_count": len(own),
                             "capture_with_cross_list_category_evidence_count": len(merged),
                             "all_captures_category_count": len(available),
                             "count_delta": delta,
                             "matches_saved_count": delta == 0 if delta is not None else None})
                visit(node.get("children", []), path)

        visit(directories[name]["roots"])
        missing_fields = defaultdict(list)
        for qid in ids:
            records = [q for n, q in occurrences[qid] if n == name]
            q = records[0]
            doc = q.get("document") or {}
            for field, value in (("stem", doc.get("stem_md") or q.get("stem")),
                                 ("answer", doc.get("answer") or q.get("answer") or q.get("correct_answer")),
                                 ("explanation", doc.get("explanation_md") or q.get("answer_explanation"))):
                if not present(value):
                    missing_fields[field].append(qid)
        lists[name] = {
            "raw_item_occurrences": len(capture["items"]), "unique_question_count": len(ids),
            "duplicate_ids_within_capture": sorted(qid for qid, count in Counter(q["id"] for q in capture["items"]).items() if count > 1),
            "active_source": selection[name]["active_source"],
            "capture_metadata": {k: v for k, v in capture.items() if k not in {"items", "directory", "metadata"}},
            "saved_definition": definitions[name],
            "saved_directory_total": directories[name].get("total"),
            "definition_item_count_delta": len(ids) - definitions[name]["item_count"],
            "directory_total_delta": len(ids) - directories[name]["total"] if isinstance(directories[name].get("total"), int) else None,
            "dimensions": dimensions(ids),
            "empty_required_content_ids": dict(missing_fields),
            "directory_nodes_checked": len(rows),
            "directory_nodes_matching_count": sum(row["matches_saved_count"] is True for row in rows),
            "directory_nodes": rows,
        }

    conflicts = []
    for qid, records in occurrences.items():
        if len(records) < 2:
            continue
        documents = {digest(q.get("document")) for _, q in records}
        hashes = {q.get("content_hash") for _, q in records}
        flags = {q.get("is_core", "missing") for _, q in records}
        if len(documents) > 1 or len(hashes) > 1 or len(flags) > 1:
            document_values = [q.get("document", {}) for _, q in records]
            changed_fields = sorted(field for field in set().union(*(doc.keys() for doc in document_values))
                                    if len({digest(doc.get(field)) for doc in document_values}) > 1)
            conflicts.append({"id": qid, "document_variant": len(documents) > 1,
                              "content_hash_variant": len(hashes) > 1, "core_flag_variant": len(flags) > 1,
                              "document_changed_fields": changed_fields,
                              "content_variant_excluding_category_and_serial": any(field not in {"category_id", "serial_number"} for field in changed_fields),
                              "variants": [{"list": n, "content_hash": q.get("content_hash"),
                                            "version_id": q.get("document", {}).get("version_id"),
                                            "document_sha256": digest(q.get("document")),
                                            "is_core": q.get("is_core", "missing"),
                                            "category_ids": sorted(category_ids(q))} for n, q in records]})
    core_ids = {qid for qid, records in occurrences.items() if records[0][1].get("is_core") is True}
    core_by_capture = {}
    for name in CAPTURES:
        core_by_capture[name] = {label: sum(q.get("is_core") is True and bool(category_ids(q) & descendants.get(cid, {cid}))
                                           for q in captures[name]["items"])
                                 for cid, label in DISCIPLINES.items()}
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "method": "Offline inspection of retained JSON captures. No requests, source edits, or inferred missing question IDs.",
        "sources": sources, "capture_selection": selection, "lists": lists,
        "baseline_union_unique_question_count": len({q["id"] for capture in baselines.values() for q in capture["items"]}),
        "union": {"unique_question_count": len(occurrences), "dimensions": dimensions(occurrences),
                  "overlap": {f"{a}_and_{b}": len(list_ids[a] & list_ids[b])
                              for i, a in enumerate(CAPTURES) for b in CAPTURES[i + 1:]},
                  "document_variant_question_count": sum(c["document_variant"] for c in conflicts),
                  "content_variant_excluding_category_and_serial_count": sum(c["content_variant_excluding_category_and_serial"] for c in conflicts),
                  "content_hash_conflict_question_count": sum(c["content_hash_variant"] for c in conflicts),
                  "core_flag_conflict_question_count": sum(c["core_flag_variant"] for c in conflicts),
                  "content_variants": conflicts},
        "core": {"unique_question_count": len(core_ids), "dimensions": dimensions(core_ids),
                 "original_record_core_by_knowledge_discipline": core_by_capture,
                 "observation": "Zero core flags in a discipline describe retained captures only; no live core total or complete core directory was retained."},
        "observations": [
            *(f"{name}: active retained unique IDs={len(list_ids[name])}; selected official item_count={definitions[name]['item_count']}; selected directory total={directories[name].get('total')}."
              for name in CAPTURES),
            "A baseline capture api_total belongs to its recorded root_category_id and must not automatically be treated as an entire official-list count. Validated recovered snapshots use full list pagination and preserve their own metadata/directory.",
            "Cross-list copies can assign different categories and document metadata to the same ID. Coverage uses the union of recorded categories but preserves each source and document variant.",
            f"Document variants={sum(c['document_variant'] for c in conflicts)}; variants beyond category_id/serial_number={sum(c['content_variant_excluding_category_and_serial'] for c in conflicts)}; content_hash conflicts={sum(c['content_hash_variant'] for c in conflicts)}; core-flag conflicts={sum(c['core_flag_variant'] for c in conflicts)}.",
            "Directory root and child counts may overlap. Summing every directory count would count a question multiple times.",
        ],
        "inferences": [
            "Any difference between selected official definitions/directories and active items indicates a coverage gap or inconsistent snapshot/count semantics; baseline scrape integrity matches prove only the requested root-filtered captures, not official list completeness.",
            "A saved-count shortfall does not establish the identities or current availability of missing questions. It must be resolved by an authorized current list/items fetch.",
        ],
        "unknowns": [
            "Current live question-list totals, revisions, unavailable IDs, and core subject coverage were not observed by this offline audit.",
            "The reason for the saved exam root/list-total discrepancy and category-count inconsistencies is not established.",
            "Nonempty answer/explanation fields do not establish mathematical correctness or image availability.",
        ],
    }
    if scan_local:
        report["local_capture_search"] = inspect_local_captures(root, set(occurrences))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--scan-local", action="store_true", help="Inspect retained raw/mirror/scrape/backups for additional question documents")
    args = parser.parse_args()
    report = audit(args.root.resolve(), scan_local=args.scan_local)
    output = args.output or args.root / "raw" / "question-coverage.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.scan_local:
        search_output = output.with_name("question-local-capture-search.json")
        search_output.write_text(json.dumps({"generated_at": report["generated_at"],
                                            **report["local_capture_search"]}, ensure_ascii=False, indent=2) + "\n",
                                 encoding="utf-8")
    print(json.dumps({"report": str(output), "unique_questions": report["union"]["unique_question_count"],
                      "core": report["core"]["unique_question_count"],
                      "list_count_deltas": {k: v["definition_item_count_delta"] for k, v in report["lists"].items()},
                      "document_variant_questions": report["union"]["document_variant_question_count"]}, ensure_ascii=True))


if __name__ == "__main__":
    main()
