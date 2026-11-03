"""Personal, offline learning workflows. Source captures remain immutable.

The one-day retry rule is a local product choice, not a recovered site algorithm.
All database connections are operation-scoped so ThreadingHTTPServer can reuse
one LocalApp safely. No third-party packages or external service is required.
"""
from __future__ import annotations

import copy
import json
import math
import os
from pathlib import Path
import random
import re
import sqlite3
import uuid
import hashlib
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

TZ = timezone(timedelta(hours=8))
CAPTURED_LISTS = ("complete", "exam", "mock")
LISTS = ("complete", "core", "exam", "mock")
SUBJECTS = ("all", "math_1", "math_2", "math_3")
MASTERY = ("not_started", "not_known", "learning", "mastered")
PAPER_DISCIPLINES = ("advanced_math", "linear_algebra", "probability")
PAPER_KINDS = {"choice": ("single_choice", "multiple_choice"), "fill": ("fill", "fill_blank"),
               "subjective": ("subjective", "essay")}
STANDARD_PAPER_RULES = tuple(
    {"discipline": discipline, "kind": kind, "count": count}
    for discipline, counts in zip(PAPER_DISCIPLINES, ((4, 4, 4), (3, 1, 1), (3, 1, 1)))
    for kind, count in zip(PAPER_KINDS, counts)
)
DEFAULT_SETTINGS = {"exam_date": "2026-12-19", "daily_goal": 20, "theme": "light", "exam_subject": "all"}


def now():
    return datetime.now(TZ).isoformat(timespec="microseconds")


class InputError(ValueError):
    pass


def integer(value, name, minimum=1, maximum=None):
    if isinstance(value, bool) or not re.fullmatch(r"\d+", str(value)):
        raise InputError(f"{name} 必须是整数")
    try:
        result = int(value)
    except (ValueError, OverflowError) as exc:
        raise InputError(f"{name} 超出允许范围") from exc
    if result < minimum or (maximum is not None and result > maximum):
        raise InputError(f"{name} 超出允许范围")
    return result


def timestamp(value, field, nullable=False):
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise InputError(f"{field} 必须是带时区的 ISO 时间")
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.astimezone(TZ).isoformat()
    except (ValueError, OverflowError) as exc:
        raise InputError(f"{field} 必须是带时区的 ISO 时间") from exc


class LocalApp:
    def __init__(self, root=None, data_dir=None):
        self.root = Path(root or Path(__file__).parent).resolve()
        self.data_dir = Path(data_dir or os.environ.get("DGU_DATA_DIR") or self.root / "user-data").resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "learning.sqlite3"
        self.questions = {}
        self.list_ids = {}
        self.directories = {}
        self.descendants = {}
        self.category_subjects = {}
        self.category_paths = {}
        for name in CAPTURED_LISTS:
            source = self.root / "scrape" / f"{name}.json"
            recovered = self.root / "mirror" / "recovered" / "questions" / f"{name}.json"
            snapshot = None
            if recovered.exists():
                snapshot = json.loads(recovered.read_text(encoding="utf-8"))
                if snapshot.get("validated_complete") is not True or snapshot.get("list_id") != name or snapshot.get("fetched") != len(snapshot.get("items", [])) or snapshot.get("api_total") != snapshot.get("fetched"):
                    raise ValueError(f"Invalid recovered question bank: {recovered}")
                source = recovered
            items = snapshot["items"] if snapshot is not None else json.loads(source.read_text(encoding="utf-8")).get("items", []) if source.exists() else []
            self.list_ids[name] = set()
            for original in items:
                qid = integer(original["id"], "question.id")
                self.list_ids[name].add(qid)
                if qid not in self.questions:
                    self.questions[qid] = copy.deepcopy(original)
                    self.questions[qid]["list_ids"] = []
                    self.questions[qid]["content_provenance"] = {"canonical_list": name, "canonical_capture": str(source), "has_document_variants": False, "variants": []}
                q = self.questions[qid]
                q["list_ids"].append(name)
                same_document = original.get("document") == q.get("document")
                provenance = q["content_provenance"]
                provenance["has_document_variants"] = provenance["has_document_variants"] or not same_document
                provenance["variants"].append({"list": name, "capture": str(source), "content_hash": original.get("content_hash"),
                                               "version_id": original.get("document", {}).get("version_id"),
                                               "source": original.get("source"), "document_matches_canonical": same_document})
                q["category_ids"] = list(dict.fromkeys(q.get("category_ids", [q.get("category_id")]) + original.get("category_ids", [])))
            directory = self.root / "mirror" / "api-data" / f"dir-{name}.json"
            roots = json.loads(directory.read_text(encoding="utf-8")).get("data", {}).get("roots", []) if directory.exists() else []
            if snapshot is not None:
                roots = snapshot["directory"]["roots"]
            self.directories[name] = roots
            self._index_categories(roots)
        category_source = self.root / "mirror" / "api-data" / "categories-complete-stats.json"
        if category_source.exists():
            self._index_categories(json.loads(category_source.read_text(encoding="utf-8")))
        # The upstream core scope selects questions carrying the original
        # is_core flag across the bank, including exam/mock-only captures.
        self.list_ids["core"] = {qid for qid, q in self.questions.items() if q.get("is_core") is True}
        for qid in self.list_ids["core"]:
            self.questions[qid]["list_ids"].append("core")
        self.directories["core"] = copy.deepcopy(self.directories["complete"])
        self.list_ids["all"] = set(self.questions)
        self.type_counts = {}
        for q in self.questions.values():
            kind = q.get("question_type") or q.get("document", {}).get("question_type") or "unknown"
            self.type_counts[kind] = self.type_counts.get(kind, 0) + 1
        for q in self.questions.values():
            subjects = set()
            unknown = False
            for cid in q.get("category_ids", []):
                found = self.category_subjects.get(str(cid))
                if found is None:
                    unknown = True
                else:
                    subjects.update(found)
            q["exam_subjects"] = sorted(subjects)
            q["subject_unknown"] = unknown or not subjects
        by_category = {}
        for qid, q in self.questions.items():
            for cid in q.get("category_ids", []):
                by_category.setdefault(str(cid), set()).add(qid)
        self.directory_question_ids = {name: {} for name in LISTS}
        for name in LISTS:
            self._index_directory_questions(name, self.directories[name], by_category)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS states (
                    question_id INTEGER PRIMARY KEY, favorite INTEGER NOT NULL DEFAULT 0,
                    mastery TEXT NOT NULL DEFAULT 'not_started', note TEXT NOT NULL DEFAULT '',
                    wrong INTEGER NOT NULL DEFAULT 0, due_at TEXT, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, question_id INTEGER NOT NULL,
                    result TEXT NOT NULL, answer TEXT NOT NULL DEFAULT '',
                    duration_seconds REAL NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
                    record_key TEXT NOT NULL UNIQUE);
                CREATE INDEX IF NOT EXISTS records_time ON records(created_at);
                CREATE INDEX IF NOT EXISTS records_question ON records(question_id);
                CREATE TABLE IF NOT EXISTS state_events (
                    event_key TEXT PRIMARY KEY, question_id INTEGER NOT NULL,
                    event_kind TEXT NOT NULL, old_value TEXT NOT NULL,
                    new_value TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS state_events_time ON state_events(created_at);
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
            if "record_key" not in {row["name"] for row in db.execute("PRAGMA table_info(records)")}:
                db.execute("ALTER TABLE records ADD COLUMN record_key TEXT")
                for row in db.execute("SELECT id FROM records"):
                    db.execute("UPDATE records SET record_key=? WHERE id=?", (uuid.uuid4().hex, row["id"]))
                db.execute("CREATE UNIQUE INDEX IF NOT EXISTS records_key ON records(record_key)")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def _index_categories(self, nodes, inherited=None, parents=()):
        ids = set()
        for node in nodes:
            cid = str(node.get("category_id", node.get("id")))
            label = node.get("label", node.get("name", ""))
            subjects = node.get("effective_exam_subjects")
            if subjects is None:
                subjects = node.get("exam_subjects")
            if subjects is None:
                subjects = inherited
            if subjects is not None:
                if isinstance(subjects, str):
                    subjects = [subjects]
                subjects = set(subjects)
                self.category_subjects[cid] = set(self.category_subjects.get(cid) or ()) | subjects
            self.category_paths.setdefault(cid, set()).add(" / ".join((*parents, label)))
            child_ids = self._index_categories(node.get("children", []), subjects, (*parents, label))
            self.descendants.setdefault(cid, set()).update({cid} | child_ids)
            ids.update({cid} | child_ids)
        return ids

    def _index_directory_questions(self, name, nodes, by_category):
        for node in nodes:
            cid = str(node.get("category_id", node.get("id")))
            ids = set()
            for descendant in self.descendants.get(cid, {cid}):
                ids.update(by_category.get(descendant, ()))
            self.directory_question_ids[name][cid] = ids & self.list_ids[name]
            self._index_directory_questions(name, node.get("children", []), by_category)

    def directory_view(self, states):
        directories = copy.deepcopy(self.directories)
        mastered = {qid for qid, state in states.items() if state["mastery"] == "mastered"}
        def annotate(name, nodes):
            for node in nodes:
                cid = str(node.get("category_id", node.get("id")))
                ids = self.directory_question_ids[name][cid]
                node["local_question_count"] = len(ids)
                node["mastered_count"] = len(ids & mastered)
                annotate(name, node.get("children", []))
        for name, roots in directories.items():
            annotate(name, roots)
        return directories

    @staticmethod
    def empty_state():
        return {"favorite": False, "mastery": "not_started", "note": "", "wrong": False, "due_at": None, "updated_at": None}

    def states(self, db):
        result = {}
        for row in db.execute("SELECT * FROM states"):
            state = dict(row)
            qid = state.pop("question_id")
            state["favorite"] = bool(state["favorite"])
            state["wrong"] = bool(state["wrong"])
            result[qid] = state
        return result

    def settings(self, db):
        result = dict(DEFAULT_SETTINGS)
        result.update({row["key"]: json.loads(row["value"]) for row in db.execute("SELECT * FROM settings")})
        return result

    def question(self, qid, states):
        q = dict(self.questions[qid])
        q["state"] = states.get(qid, self.empty_state())
        return q

    def selection(self, params, states):
        list_name = params.get("list", "all")
        if not isinstance(list_name, str) or list_name not in self.list_ids:
            raise InputError("题单无效")
        kind = params.get("type", "all")
        if not isinstance(kind, str) or (kind not in ("all", "") and kind not in self.type_counts):
            raise InputError("题型无效")
        subject = params.get("subject", "all")
        if subject not in SUBJECTS:
            raise InputError("考试科目无效")
        filter_name = params.get("filter", "all")
        if filter_name not in ("all", "favorites", "wrong", "notes", "due", *MASTERY):
            raise InputError("筛选方式无效")
        category = str(params.get("category", ""))
        category_ids = None
        if category not in ("", "all", "0"):
            integer(category, "category")
            category_ids = self.descendants.get(category, {category})
        query = str(params.get("q", "")).strip().casefold()
        numeric_query = re.fullmatch(r"#?(\d+)", query)
        # Bare modern years search captured sources/text. Prefix a number with
        # # for an unambiguous question ID, including IDs that resemble years.
        year_query = re.fullmatch(r"(?:19|20)\d{2}", query)
        exact_id = integer(numeric_query[1], "question_id") if numeric_query and not year_query else None
        selected = []
        current = now()
        for qid, q in self.questions.items():
            if exact_id is not None and qid != exact_id:
                continue
            if qid not in self.list_ids[list_name]:
                continue
            state = states.get(qid, self.empty_state())
            if kind not in ("", "all") and q.get("question_type", q.get("document", {}).get("question_type")) != kind:
                continue
            if category_ids is not None and not any(str(cid) in category_ids for cid in q.get("category_ids", [])):
                continue
            if subject != "all" and not q["subject_unknown"] and subject not in q["exam_subjects"]:
                continue
            if filter_name == "favorites" and not state["favorite"]:
                continue
            if filter_name == "wrong" and not state["wrong"]:
                continue
            if filter_name == "notes" and not state["note"].strip():
                continue
            if filter_name in MASTERY and state["mastery"] != filter_name:
                continue
            if filter_name == "due" and not (state["due_at"] and state["due_at"] <= current):
                continue
            if query and exact_id is None:
                texts = [q.get("stem", ""), q.get("document", {}).get("stem_md", ""), q.get("source", ""), q.get("original_source", ""), q.get("category_name", ""), q.get("category_full_path", ""), state["note"]]
                texts.extend(variant.get("source") or "" for variant in q["content_provenance"]["variants"])
                texts.extend(path for cid in q.get("category_ids", []) for path in self.category_paths.get(str(cid), ()))
                if query not in " ".join(str(t) for t in texts).casefold():
                    continue
            selected.append(qid)
        return selected

    @staticmethod
    def paginate(items, params):
        page = integer(params.get("page", 1), "page")
        per_page = integer(params.get("per_page", 12), "per_page", maximum=200)
        return {"items": items[(page - 1) * per_page:page * per_page], "total": len(items), "page": page, "total_pages": math.ceil(len(items) / per_page)}

    def stats(self, db, states):
        today = datetime.now(TZ).date()
        rows = db.execute("SELECT COUNT(*) total, COUNT(DISTINCT question_id) practiced, SUM(result='correct') correct FROM records").fetchone()
        activity = []
        for ago in range(6, -1, -1):
            day = (today - timedelta(days=ago)).isoformat()
            row = db.execute("SELECT COUNT(*) total, SUM(result='correct') correct FROM records WHERE substr(created_at,1,10)=?", (day,)).fetchone()
            activity.append({"date": day, "attempts": row["total"], "correct": row["correct"] or 0})
        last = db.execute("SELECT question_id,created_at FROM records ORDER BY created_at DESC,id DESC LIMIT 1").fetchone()
        daily = {row["day"]: {"attempts": row["total"], "correct": row["correct"] or 0}
                 for row in db.execute("SELECT substr(created_at,1,10) day,COUNT(*) total,SUM(result='correct') correct FROM records GROUP BY day")}
        first_day = date(today.year, 1, 1)
        activity_year = [{"date": day.isoformat(), **daily.get(day.isoformat(), {"attempts": 0, "correct": 0})}
                         for day in (first_day + timedelta(days=i) for i in range((date(today.year + 1, 1, 1) - first_day).days))]
        streak_day = today if today.isoformat() in daily else today - timedelta(days=1)
        streak = 0
        while streak_day.isoformat() in daily:
            streak += 1
            streak_day -= timedelta(days=1)
        return {"today_attempts": activity[-1]["attempts"], "practiced": rows["practiced"],
                "mastered": sum(s["mastery"] == "mastered" for s in states.values()),
                "favorites": sum(s["favorite"] for s in states.values()), "wrong": sum(s["wrong"] for s in states.values()),
                "due": sum(bool(s["due_at"] and s["due_at"] <= now()) for s in states.values()),
                "notes": sum(bool(s["note"].strip()) for s in states.values()),
                "correct_rate": round((rows["correct"] or 0) / rows["total"] * 100, 1) if rows["total"] else 0,
                "total_attempts": rows["total"], "activity": activity, "last_question_id": last[0] if last else None,
                "activity_year": activity_year, "streak": streak, "last_activity_at": last[1] if last else None}

    def question_discipline(self, qid):
        """Use captured directory ancestry; unknown ancestry remains unclassified."""
        question = self.questions[qid]
        paths = [path for cid in question.get("category_ids", []) for path in self.category_paths.get(str(cid), ())]
        for label, discipline in (("高等数学", "advanced_math"), ("线性代数", "linear_algebra"), ("概率", "probability")):
            if any(label in path.split(" / ")[0] for path in paths):
                return discipline
        return None

    def paper(self, body, states, check=False):
        if not isinstance(body, dict):
            raise InputError("组卷条件必须是对象")
        mode = body.get("mode", "custom")
        if mode not in ("standard", "custom"):
            raise InputError("组卷模式无效")
        priorities = {}
        for field in ("prioritize_wrong", "prioritize_favorites"):
            value = body.get(field, False)
            if type(value) is not bool:
                raise InputError(f"{field} 必须是布尔值")
            priorities[field] = value
        ids = self.selection(body, states)
        rules = STANDARD_PAPER_RULES if mode == "standard" else body.get("rules")
        if rules is None:
            rules = [{"count": integer(body.get("count", 20), "count", maximum=200)}]
        if not isinstance(rules, (list, tuple)) or not rules:
            raise InputError("组卷规则必须是非空数组")
        parsed = []
        for rule in rules:
            if not isinstance(rule, dict):
                raise InputError("组卷规则必须是对象")
            discipline, kind = rule.get("discipline"), rule.get("kind")
            if discipline is not None and discipline not in PAPER_DISCIPLINES:
                raise InputError("组卷学科无效")
            if kind is not None and (not isinstance(kind, str) or kind not in PAPER_KINDS):
                raise InputError("组卷题型无效")
            parsed.append({"discipline": discipline, "kind": kind, "count": integer(rule.get("count"), "count", minimum=0, maximum=200)})
        if sum(rule["count"] for rule in parsed) > 200:
            raise InputError("组卷总题数不可超过200")
        selected, groups, shortages = [], [], []
        used = set()
        candidate_ids = set()
        for rule in parsed:
            candidates = [qid for qid in ids if qid not in used
                          and (rule["discipline"] is None or self.question_discipline(qid) == rule["discipline"])
                          and (rule["kind"] is None or self.questions[qid].get("question_type", self.questions[qid].get("document", {}).get("question_type")) in PAPER_KINDS[rule["kind"]])]
            candidate_ids.update(candidates)
            preferred = [qid for qid in candidates if (priorities["prioritize_wrong"] and states.get(qid, {}).get("wrong"))
                         or (priorities["prioritize_favorites"] and states.get(qid, {}).get("favorite"))]
            preferred_ids = set(preferred)
            rest = [qid for qid in candidates if qid not in preferred_ids]
            chosen = random.sample(preferred, min(rule["count"], len(preferred)))
            chosen += random.sample(rest, min(rule["count"] - len(chosen), len(rest)))
            used.update(chosen)
            selected.extend(chosen)
            group = {**rule, "available": len(candidates), "selected": len(chosen), "question_ids": chosen}
            groups.append(group)
            if len(chosen) < rule["count"]:
                shortages.append({**rule, "available": len(candidates), "missing": rule["count"] - len(chosen)})
        result = {"total": len(selected), "available": len(ids), "requested_total": sum(r["count"] for r in parsed),
                  "expected_total": len(selected), "groups": groups, "shortages": shortages}
        if check:
            result["candidates"] = [qid for qid in ids if qid in candidate_ids]
        else:
            result["items"] = [self.question(qid, states) for qid in selected]
        return result

    def validate_state(self, state, partial=False):
        if not isinstance(state, dict):
            raise InputError("state 必须是对象")
        fields = ("favorite", "mastery", "note") if partial else ("favorite", "mastery", "note", "wrong", "due_at", "updated_at")
        if any(k not in fields for k in state):
            raise InputError("state 含有未知字段")
        result = dict(state)
        for key in ("favorite", "wrong"):
            if key in result and type(result[key]) is not bool:
                raise InputError(f"{key} 必须是布尔值")
        if "mastery" in result and result["mastery"] not in MASTERY:
            raise InputError("掌握度无效")
        if "note" in result and not isinstance(result["note"], str):
            raise InputError("笔记必须是文本")
        for key in ("updated_at", "due_at"):
            if key in result:
                result[key] = timestamp(result[key], key, nullable=True)
        return result

    def validate_settings(self, settings):
        if not isinstance(settings, dict) or any(k not in DEFAULT_SETTINGS for k in settings):
            raise InputError("设置字段无效")
        result = dict(settings)
        if "daily_goal" in result:
            result["daily_goal"] = integer(result["daily_goal"], "daily_goal", maximum=200)
        if "theme" in result and result["theme"] not in ("light", "dark"):
            raise InputError("主题无效")
        if "exam_subject" in result and result["exam_subject"] not in SUBJECTS:
            raise InputError("考试科目无效")
        if "exam_date" in result:
            if not isinstance(result["exam_date"], str):
                raise InputError("考试日期无效")
            if result["exam_date"]:
                try:
                    parsed = date.fromisoformat(result["exam_date"])
                    if parsed.isoformat() != result["exam_date"] or not 2000 <= parsed.year <= 2100:
                        raise ValueError()
                except ValueError as exc:
                    raise InputError("考试日期应为 YYYY-MM-DD，年份为 2000–2100") from exc
        return result

    def validate_attempt(self, body):
        if not isinstance(body, dict) or body.get("result") not in ("correct", "wrong"):
            raise InputError("练习结果必须是 correct 或 wrong")
        answer = body.get("answer", "")
        duration = body.get("duration_seconds", 0)
        if not isinstance(answer, str):
            raise InputError("答案必须是文本")
        try:
            valid_duration = not isinstance(duration, bool) and isinstance(duration, (int, float)) and math.isfinite(duration) and duration >= 0
        except OverflowError:
            valid_duration = False
        if not valid_duration:
            raise InputError("练习时长必须是非负有限数")
        return {"result": body["result"], "answer": answer, "duration_seconds": duration}

    def save_state(self, db, qid, state):
        db.execute("""INSERT INTO states VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(question_id) DO UPDATE SET favorite=excluded.favorite, mastery=excluded.mastery,
            note=excluded.note,wrong=excluded.wrong,due_at=excluded.due_at,updated_at=excluded.updated_at""",
            (qid, state["favorite"], state["mastery"], state["note"], state["wrong"], state["due_at"], state["updated_at"] or now()))

    @staticmethod
    def record_state_changes(db, qid, before, after):
        for field in ("favorite", "mastery"):
            if before[field] != after[field]:
                db.execute("INSERT INTO state_events VALUES(?,?,?,?,?,?)",
                           (uuid.uuid4().hex, qid, field, json.dumps(before[field]), json.dumps(after[field]), after["updated_at"]))

    @staticmethod
    def event_view(row):
        event = dict(row)
        event["old_value"] = json.loads(event["old_value"])
        event["new_value"] = json.loads(event["new_value"])
        event["action"] = ("favorite" if event["new_value"] else "unfavorite") if event["event_kind"] == "favorite" else event["new_value"]
        return event

    @staticmethod
    def save_settings(db, settings):
        db.executemany("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       [(k, json.dumps(v, ensure_ascii=False)) for k, v in settings.items()])

    def import_data(self, db, body):
        if not isinstance(body, dict) or type(body.get("schema_version")) is not int or body.get("schema_version") != 1:
            raise InputError("不支持的备份格式")
        if not isinstance(body.get("states"), dict) or not isinstance(body.get("records"), list):
            raise InputError("备份 states/records 格式无效")
        settings = self.validate_settings(body.get("settings", {}))
        imported_states = {}
        for raw_id, raw in body["states"].items():
            qid = integer(raw_id, "question_id")
            if qid not in self.questions:
                raise InputError(f"题目不存在: {qid}")
            imported_states[qid] = self.validate_state(raw)
        records = []
        events = []
        raw_events = body.get("state_events", [])
        if not isinstance(raw_events, list):
            raise InputError("备份 state_events 格式无效")
        seen_events = {}
        for raw in raw_events:
            if not isinstance(raw, dict) or raw.get("event_kind") not in ("favorite", "mastery"):
                raise InputError("状态记录格式无效")
            qid = integer(raw.get("question_id"), "question_id")
            if qid not in self.questions:
                raise InputError(f"题目不存在: {qid}")
            field = raw["event_kind"]
            for value in (raw.get("old_value"), raw.get("new_value")):
                self.validate_state({field: value}, partial=True)
            key = raw.get("event_key")
            if not isinstance(key, str) or not key:
                raise InputError("event_key 必须是非空字符串")
            event = (key, qid, field, json.dumps(raw["old_value"]), json.dumps(raw["new_value"]), timestamp(raw.get("created_at"), "created_at"))
            existing = db.execute("SELECT * FROM state_events WHERE event_key=?", (key,)).fetchone()
            if (key in seen_events and seen_events[key] != event) or (existing is not None and tuple(existing) != event):
                raise InputError("同一 event_key 的状态记录内容冲突")
            seen_events[key] = event
            events.append(event)
        legacy_occurrences = {}
        record_keys = {}
        for raw in body["records"]:
            if not isinstance(raw, dict):
                raise InputError("练习记录格式无效")
            qid = integer(raw.get("question_id"), "question_id")
            if qid not in self.questions:
                raise InputError(f"题目不存在: {qid}")
            attempt = self.validate_attempt(raw)
            record = (qid, attempt["result"], attempt["answer"], attempt["duration_seconds"], timestamp(raw.get("created_at"), "created_at"))
            key = raw.get("record_key")
            if key is None:
                # Legacy records lack stable IDs. Keep repeated identical attempts
                # distinct, while importing the same legacy file remains idempotent.
                digest = hashlib.sha256(json.dumps(record, ensure_ascii=False).encode()).hexdigest()
                occurrence = legacy_occurrences.get(digest, 0)
                legacy_occurrences[digest] = occurrence + 1
                key = f"legacy-{digest}-{occurrence}"
            if not isinstance(key, str) or not key:
                raise InputError("record_key 必须是非空字符串")
            if key in record_keys and record_keys[key] != record:
                raise InputError("同一 record_key 的记录内容冲突")
            record_keys[key] = record
            existing = db.execute("SELECT question_id,result,answer,duration_seconds,created_at FROM records WHERE record_key=?", (key,)).fetchone()
            if existing is not None and tuple(existing) != record:
                raise InputError("导入记录与本地同一 record_key 的记录内容冲突")
            records.append((*record, key))
        backup_dir = self.data_dir / "backups"
        backup_dir.mkdir(exist_ok=True)
        backup = backup_dir / ("learning-" + datetime.now(TZ).strftime("%Y%m%d-%H%M%S-%f") + ".sqlite3")
        destination = sqlite3.connect(backup)
        try:
            db.backup(destination)
        finally:
            destination.close()
        with db:
            db.execute("BEGIN IMMEDIATE")
            existing_states = self.states(db)
            skipped_older = 0
            merged_count = 0
            for qid, imported in imported_states.items():
                existing = existing_states.get(qid)
                # Merge is a backup-safe operation: stale snapshots must not
                # roll newer learning work back. Equal timestamps can still
                # merge explicit edits; unknown timestamps cannot replace a
                # state whose update time is known.
                if existing and existing["updated_at"] and (not imported.get("updated_at") or imported["updated_at"] < existing["updated_at"]):
                    skipped_older += 1
                    continue
                merged = dict(existing_states.get(qid, self.empty_state()))
                merged.update(imported)
                self.save_state(db, qid, merged)
                merged_count += 1
            self.save_settings(db, settings)
            db.executemany("INSERT OR IGNORE INTO state_events VALUES(?,?,?,?,?,?)", events)
            for record in records:
                # Re-importing the same backup must not double practice totals.
                existing = db.execute("SELECT question_id,result,answer,duration_seconds,created_at FROM records WHERE record_key=?", (record[-1],)).fetchone()
                exists = existing is not None
                if not exists:
                    db.execute("INSERT INTO records(question_id,result,answer,duration_seconds,created_at,record_key) VALUES(?,?,?,?,?,?)", record)
        return {"states_imported": merged_count, "states_skipped_older": skipped_older, "records_imported": len(records), "state_events_imported": len(events), "backup_path": str(backup)}

    def dispatch(self, method, path, params=None, body=None):
        params = {k: (v[0] if isinstance(v, list) and v else v) for k, v in (params or {}).items()}
        method = method.upper()
        try:
            with self.connect() as db:
                if method in ("PUT", "POST") and path.rstrip("/") not in ("/api/local/import", "/api/local/paper", "/api/local/paper/check"):
                    # Acquire the writer lock before reading a state to merge.
                    # Concurrent note/favorite/attempt writes cannot overwrite
                    # fields using an older snapshot of that question.
                    db.execute("BEGIN IMMEDIATE")
                data = self._dispatch(db, method, path.rstrip("/"), params, body)
            return 200, {"code": 0, "data": data}
        except InputError as exc:
            return 400, {"code": 400, "message": str(exc), "data": None}
        except KeyError:
            return 404, {"code": 404, "message": "题目或接口不存在", "data": None}

    def _dispatch(self, db, method, path, params, body):
        states = self.states(db)
        if method == "GET" and path == "/api/local/bootstrap":
            return {"counts": {"total": len(self.questions), "list_counts": {n: len(self.list_ids[n]) for n in LISTS}},
                    "total": len(self.questions), "list_counts": {n: len(self.list_ids[n]) for n in LISTS},
                    "type_counts": self.type_counts,
                    "directories": self.directory_view(states), "stats": self.stats(db, states), "settings": self.settings(db)}
        if method == "GET" and path == "/api/local/questions":
            ids = self.selection(params, states)
            if params.get("filter") == "favorites":
                order = params.get("sort", "new")
                if order not in ("new", "old"):
                    raise InputError("收藏排序无效")
                favorited_at = {row["question_id"]: row["created_at"] for row in db.execute(
                    "SELECT question_id,MAX(created_at) created_at FROM state_events WHERE event_kind='favorite' AND new_value='true' GROUP BY question_id")}
                ids.sort(key=lambda qid: (favorited_at.get(qid, states[qid]["updated_at"] or ""), qid), reverse=order == "new")
            page = self.paginate(ids, params)
            page["items"] = [self.question(qid, states) for qid in page["items"]]
            return page
        if method == "GET" and path == "/api/local/map":
            ids = set(self.selection(params, states))
            items = []
            for index, (qid, question) in enumerate(self.questions.items(), start=1):
                if qid not in ids:
                    continue
                state = states.get(qid, self.empty_state())
                items.append({"id": qid, "index": index, "category_id": question.get("category_id", next(iter(question.get("category_ids", [])), None)),
                              "mastery": state["mastery"], "favorite": state["favorite"], "has_note": bool(state["note"].strip())})
            return {"items": items, "total": len(items)}
        match = re.fullmatch(r"/api/local/questions/([^/]+)(?:/(state|attempt))?", path)
        if match:
            qid = integer(match[1], "question_id")
            if qid not in self.questions:
                raise KeyError(qid)
            action = match[2]
            if method == "GET" and not action:
                return self.question(qid, states)
            state = dict(states.get(qid, self.empty_state()))
            before = dict(state)
            if method == "PUT" and action == "state":
                changes = self.validate_state(body, partial=True)
                state.update(changes)
                if "mastery" in changes:
                    state["wrong"] = state["mastery"] in ("not_known", "learning")
                    state["due_at"] = (datetime.now(TZ) + timedelta(days=1)).isoformat(timespec="seconds") if state["wrong"] else None
                state["updated_at"] = now()
                self.save_state(db, qid, state)
                self.record_state_changes(db, qid, before, state)
                return state
            if method == "POST" and action == "attempt":
                attempt = self.validate_attempt(body)
                created = now()
                record_key = uuid.uuid4().hex
                cursor = db.execute("INSERT INTO records(question_id,result,answer,duration_seconds,created_at,record_key) VALUES(?,?,?,?,?,?)",
                                    (qid, attempt["result"], attempt["answer"], attempt["duration_seconds"], created, record_key))
                state["wrong"] = attempt["result"] == "wrong"
                state["due_at"] = (datetime.now(TZ) + timedelta(days=1)).isoformat(timespec="seconds") if state["wrong"] else None
                state["mastery"] = "learning" if state["wrong"] or state["mastery"] != "mastered" else "mastered"
                state["updated_at"] = created
                self.save_state(db, qid, state)
                return {"record": {"id": cursor.lastrowid, "question_id": qid, **attempt, "created_at": created, "record_key": record_key}, "state": state}
        if method == "GET" and path == "/api/local/records":
            page = integer(params.get("page", 1), "page")
            size = integer(params.get("per_page", 20), "per_page", maximum=200)
            record_type = params.get("record_type", "attempt")
            if record_type not in ("attempt", "all", "favorite", *MASTERY):
                raise InputError("记录类型无效")
            records = []
            if record_type in ("attempt", "all"):
                for row in db.execute("SELECT * FROM records"):
                    records.append({**dict(row), "event_kind": "attempt", "action": row["result"]})
            if record_type != "attempt":
                for row in db.execute("SELECT * FROM state_events"):
                    event = self.event_view(row)
                    if record_type == "all" or (record_type == "favorite" and event["event_kind"] == "favorite") or (event["event_kind"] == "mastery" and event["new_value"] == record_type):
                        records.append(event)
            bounds = {}
            for field in ("start", "end"):
                value = params.get(field)
                if value:
                    try:
                        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                            raise ValueError()
                        bounds[field] = date.fromisoformat(value).isoformat()
                    except (ValueError, TypeError) as exc:
                        raise InputError("记录日期必须是 YYYY-MM-DD") from exc
            if bounds.get("start", "") > bounds.get("end", "9999-12-31"):
                raise InputError("开始日期不能晚于结束日期")
            order = params.get("sort", "new")
            if order not in ("new", "old"):
                raise InputError("记录排序无效")
            records = [record for record in records
                       if bounds.get("start", "") <= record["created_at"][:10] <= bounds.get("end", "9999-12-31")]
            records.sort(key=lambda record: (record["created_at"], str(record.get("record_key") or record.get("event_key"))), reverse=order == "new")
            total = len(records)
            offset = (page - 1) * size
            rows = records[offset:offset + size] if offset < total else ()
            records = []
            for record in rows:
                q = self.questions.get(record["question_id"], {})
                record.update({"stem": q.get("stem", ""), "source": q.get("source", ""), "question_type": q.get("question_type", "")})
                record["question"] = {"id": record["question_id"], "stem": record["stem"], "source": record["source"], "question_type": record["question_type"]}
                records.append(record)
            return {"items": records, "total": total, "page": page, "total_pages": math.ceil(total / size)}
        if method == "POST" and path in ("/api/local/paper", "/api/local/paper/check"):
            return self.paper(body, states, check=path.endswith("/check"))
        if method == "PUT" and path == "/api/local/settings":
            self.save_settings(db, self.validate_settings(body))
            return self.settings(db)
        if method == "GET" and path == "/api/local/export":
            return {"schema_version": 1, "exported_at": now(), "states": {str(k): v for k, v in states.items()},
                    "records": [dict(r) for r in db.execute("SELECT * FROM records ORDER BY created_at,id")],
                    "state_events": [self.event_view(r) for r in db.execute("SELECT * FROM state_events ORDER BY created_at,event_key")], "settings": self.settings(db)}
        if method == "POST" and path == "/api/local/import":
            return self.import_data(db, body)
        raise KeyError(path)
