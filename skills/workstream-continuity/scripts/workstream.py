from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from workstream_core import (
    active_conclusions,
    audit_conclusions,
    compile_dashboard,
    conclusions_path,
    effective_conclusions,
    load_registry,
    promote_conclusion,
    read_conclusions,
    record_conclusion,
    record_validity,
    registry_search_text,
    workstream_metadata,
    workstream_lock,
)


for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")


DEFAULT_ROOT = Path(os.environ.get("CODEX_WORKSTREAM_ROOT", r"E:\CodexWorkstreams"))
CONTEXT_REQUIRED = {
    "schema_version",
    "workstream_id",
    "title",
    "project_root",
    "status",
    "aliases",
    "objective",
    "current_intent",
    "scope",
    "frozen_decisions",
    "open_loops",
    "artifacts",
    "resume",
    "updated_at",
}
FORBIDDEN_CONTEXT_KEYS = {
    "transcript",
    "messages",
    "chat_history",
    "raw_log",
    "pid",
    "heartbeat",
    "progress",
    "eta",
    "stage",
    "verified_at",
}
VOLATILE_FINAL_KEYS = {"pid", "heartbeat", "eta", "verified_at", "last_verified"}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8-sig") as handle:
        value = yaml.safe_load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return value


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def dump_yaml(path: Path, value: dict[str, Any]) -> None:
    atomic_text(
        path,
        yaml.safe_dump(
            value,
            allow_unicode=True,
            sort_keys=False,
            width=100,
        ),
    )


def dump_json(path: Path, value: Any) -> None:
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def context_path(root: Path, workstream_id: str) -> Path:
    return root / "workstreams" / workstream_id / "context.yaml"


def runtime_path(root: Path, workstream_id: str) -> Path:
    return root / "runtime" / f"{workstream_id}.json"


def iter_contexts(root: Path):
    base = root / "workstreams"
    if not base.exists():
        return
    for path in sorted(base.glob("*/context.yaml")):
        try:
            yield load_yaml(path)
        except Exception as exc:
            print(f"WARN unreadable context {path}: {exc}", file=sys.stderr)


def active_ids(root: Path) -> list[str]:
    value = load_json(root / "runtime" / "active.json", {"active": []})
    ids = value.get("active", []) if isinstance(value, dict) else []
    return [str(item) for item in ids]


def write_active_ids(root: Path, ids: list[str]) -> None:
    unique = list(dict.fromkeys(ids))
    dump_json(
        root / "runtime" / "active.json",
        {"active": unique, "updated_at": now_iso()},
    )


def pid_alive(pid: Any) -> bool | None:
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return None
    if pid_int <= 0:
        return False
    if os.name == "nt":
        process_query_limited_information = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            process_query_limited_information, False, pid_int
        )
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid_int, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def command_list(args: argparse.Namespace) -> int:
    active = set(active_ids(args.root))
    rows = []
    for context in iter_contexts(args.root):
        workstream_id = str(context.get("workstream_id", ""))
        metadata = workstream_metadata(args.root, workstream_id)
        rows.append(
            {
                "workstream_id": workstream_id,
                "title": context.get("title"),
                "status": context.get("status"),
                "active_pointer": workstream_id in active,
                "primary_project_id": metadata.get("primary_project_id"),
                "conclusion_count": metadata.get("conclusion_count", 0),
                "updated_at": context.get("updated_at"),
            }
        )
    print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
    return 0


def searchable_text(context: dict[str, Any], root: Path | None = None) -> str:
    chunks: list[str] = [
        str(context.get("workstream_id", "")),
        str(context.get("title", "")),
        str(context.get("objective", "")),
        str(context.get("current_intent", "")),
    ]
    for key in ("aliases", "scope", "frozen_decisions", "open_loops"):
        chunks.extend(str(item) for item in context.get(key, []) or [])
    chunks.extend(str(item) for item in (context.get("artifacts", {}) or {}).values())
    chunks.extend(str(item) for item in context.get("dataset_ids", []) or [])
    if root is not None:
        chunks.append(registry_search_text(root, str(context.get("workstream_id", ""))))
    return "\n".join(chunks).casefold()


def semantic_terms(value: str) -> set[str]:
    value = value.casefold()
    terms = {item for item in re.findall(r"[a-z0-9_]+", value) if len(item) >= 2}
    for sequence in re.findall(r"[\u3400-\u9fff]+", value):
        if len(sequence) == 1:
            terms.add(sequence)
        else:
            terms.update(sequence[index : index + 2] for index in range(len(sequence) - 1))
    return terms


def command_resolve(args: argparse.Namespace) -> int:
    contexts = [
        context
        for context in iter_contexts(args.root)
        if args.include_closed or context.get("status") != "closed"
    ]
    active = set(active_ids(args.root))
    query = args.query.strip().casefold()
    if not query:
        selected = [c for c in contexts if c.get("workstream_id") in active]
        if len(selected) == 1:
            workstream_id = str(selected[0].get("workstream_id"))
            print(
                json.dumps(
                    {"workstream_id": workstream_id, "title": selected[0].get("title"),
                     "context_path": str(context_path(args.root, workstream_id))},
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                )
            )
            return 0
        print(json.dumps({"matches": []}, ensure_ascii=False, indent=2))
        return 2

    query_terms = semantic_terms(query)
    scored = []
    for context in contexts:
        haystack = searchable_text(context, args.root)
        score = 0
        workstream_id = str(context.get("workstream_id", "")).casefold()
        title = str(context.get("title", "")).casefold()
        aliases = [str(item).casefold() for item in context.get("aliases", []) or []]
        if query in haystack:
            score += 20
        if workstream_id and workstream_id in query:
            score += 20
        if title and (title in query or query in title):
            score += 15
        score += 10 * sum(1 for alias in aliases if alias and alias in query)
        score += 2 * len(query_terms & semantic_terms(haystack))
        if context.get("workstream_id") in active:
            score += 3
        project_root = str(context.get("project_root", ""))
        if args.cwd and project_root:
            try:
                cwd = Path(args.cwd).resolve()
                root_path = Path(project_root).resolve()
                if cwd == root_path or root_path in cwd.parents:
                    score += 12
            except (OSError, ValueError):
                pass
        if score:
            scored.append((score, context))
    scored.sort(key=lambda item: (item[0], str(item[1].get("updated_at", ""))), reverse=True)
    output = []
    for score, context in scored[: args.limit]:
        workstream_id = str(context.get("workstream_id"))
        output.append(
            {
            "score": score,
            "workstream_id": workstream_id,
            "title": context.get("title"),
            "context_path": str(
                context_path(args.root, workstream_id)
            ),
        }
        )
    print(json.dumps({"matches": output}, ensure_ascii=False, indent=2))
    if not output:
        return 2
    if len(scored) > 1 and scored[0][0] - scored[1][0] < 5:
        return 3
    return 0


def audit_context(context: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    missing = sorted(CONTEXT_REQUIRED - set(context))
    if missing:
        errors.append(f"missing required fields: {', '.join(missing)}")
    forbidden = sorted(FORBIDDEN_CONTEXT_KEYS & set(context))
    if forbidden:
        errors.append(f"volatile or historical fields in context: {', '.join(forbidden)}")
    if context.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    if context.get("status") not in {"active", "paused", "closed"}:
        errors.append("status must be active, paused, or closed")
    workstream_id = str(context.get("workstream_id", ""))
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", workstream_id):
        errors.append("workstream_id must be kebab-case")
    limits = {
        "aliases": 20,
        "frozen_decisions": 30,
        "open_loops": 20,
        "artifacts": 40,
        "dataset_ids": 40,
        "current_conclusion_ids": 20,
    }
    for key, limit in limits.items():
        value = context.get(key, {})
        if len(value or []) > limit:
            errors.append(f"{key} exceeds {limit} entries")
    if len(str(context.get("objective", ""))) > 1200:
        errors.append("objective exceeds 1200 characters")
    if len(str(context.get("current_intent", ""))) > 800:
        errors.append("current_intent exceeds 800 characters")
    for key in (
        "aliases",
        "scope",
        "frozen_decisions",
        "open_loops",
        "dataset_ids",
        "current_conclusion_ids",
    ):
        values = [str(item).strip() for item in context.get(key, []) or []]
        if any(not value for value in values):
            errors.append(f"{key} contains blank items")
        if len(values) != len(set(values)):
            errors.append(f"{key} contains duplicate items")
    return errors


def audit_registry_link(root: Path, context: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    registry = load_registry(root)
    workstream_id = str(context.get("workstream_id", ""))
    meta = (registry.get("workstreams", {}) or {}).get(workstream_id)
    if not isinstance(meta, dict):
        return [f"registry missing workstream {workstream_id}"]
    projects = registry.get("projects", {}) or {}
    primary = meta.get("primary_project_id")
    if not primary or primary not in projects:
        errors.append(f"registry primary_project_id is missing or unknown: {primary}")
    for project_id in meta.get("related_project_ids", []) or []:
        if project_id not in projects:
            errors.append(f"registry related project is unknown: {project_id}")
    if context.get("project_id") and context.get("project_id") != primary:
        errors.append("context project_id does not match registry primary_project_id")
    if primary in projects:
        roots = [str(item) for item in projects[primary].get("roots", []) or []]
        project_root = os.path.normcase(os.path.normpath(str(context.get("project_root", ""))))
        if roots and project_root not in {
            os.path.normcase(os.path.normpath(item)) for item in roots
        }:
            errors.append("context project_root is not a registered primary project root")
    return errors


def command_audit(args: argparse.Namespace) -> int:
    path = context_path(args.root, args.workstream_id)
    if not path.exists():
        print(f"ERROR missing {path}", file=sys.stderr)
        return 2
    context = load_yaml(path)
    context_errors = [*audit_context(context), *audit_registry_link(args.root, context)]
    conclusion_audit = audit_conclusions(args.root, args.workstream_id)
    from workstream_memory import continuation_context
    memory = continuation_context(args.root, args.workstream_id)
    errors = [*context_errors, *conclusion_audit.get("errors", [])]
    result = {
        "workstream_id": args.workstream_id,
        "valid": not errors,
        "errors": errors,
        "warnings": [*conclusion_audit.get("warnings", []), *(
            ["context is above the 4000-character observation target; review relevance without silently deleting facts"]
            if len(path.read_text(encoding="utf-8-sig")) > 4000 else []
        )],
        "memory": {key: memory.get(key) for key in ("freshness", "pending_merges", "coverage_warnings") if key in memory},
        "conclusions": {
            key: conclusion_audit.get(key)
            for key in ("count", "active_count", "pending_promotions", "pending_validity_events", "state_basis_status")
        },
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


def command_status(args: argparse.Namespace) -> int:
    context = load_yaml(context_path(args.root, args.workstream_id))
    runtime = load_json(runtime_path(args.root, args.workstream_id), {})
    conclusion_audit = audit_conclusions(args.root, args.workstream_id)
    result = {
        "workstream_id": args.workstream_id,
        "title": context.get("title"),
        "semantic_status": context.get("status"),
        "current_intent": context.get("current_intent"),
        "open_loops": context.get("open_loops", []),
        "semantic_state": {
            **workstream_metadata(args.root, args.workstream_id),
            **{
                key: conclusion_audit.get(key)
                for key in (
                    "valid",
                    "count",
                    "active_count",
                    "pending_promotions",
                    "latest_conclusion",
                )
            },
        },
        "runtime": runtime,
    }
    if args.probe_pid:
        result["runtime"]["pid_alive"] = pid_alive(runtime.get("pid"))
        result["runtime"]["probed_at"] = now_iso()
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


def command_checkpoint(args: argparse.Namespace) -> int:
    context = load_yaml(context_path(args.root, args.workstream_id))
    if context.get("status") == "closed":
        print("ERROR cannot checkpoint a closed workstream", file=sys.stderr)
        return 2
    path = runtime_path(args.root, args.workstream_id)
    runtime = load_json(path, {})
    for key in ("stage", "completed", "total", "errors", "pid", "eta", "note"):
        value = getattr(args, key)
        if value is not None:
            runtime[key] = value
    runtime["verified_at"] = now_iso()
    dump_json(path, runtime)
    ids = active_ids(args.root)
    if args.workstream_id not in ids:
        ids.append(args.workstream_id)
        write_active_ids(args.root, ids)
    print(json.dumps(runtime, ensure_ascii=False, indent=2))
    return 0


def command_set_active(args: argparse.Namespace) -> int:
    context = load_yaml(context_path(args.root, args.workstream_id))
    if context.get("status") == "closed":
        print("ERROR cannot activate a closed workstream", file=sys.stderr)
        return 2
    ids = active_ids(args.root)
    if args.workstream_id not in ids:
        ids.append(args.workstream_id)
    write_active_ids(args.root, ids)
    print(json.dumps({"active": ids}, ensure_ascii=False, indent=2))
    return 0


def parse_key_values(values: list[str] | None, field: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for item in values or []:
        if "=" not in item:
            raise ValueError(f"{field} values must use KEY=VALUE")
        key, raw = item.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"{field} contains a blank key")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            value = raw
        result[key] = value
    return result


def conclusion_payload(args: argparse.Namespace) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if args.input:
        if args.input == "-":
            raw = sys.stdin.read()
        else:
            raw = Path(args.input).read_text(encoding="utf-8-sig")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("conclusion input must be a JSON object")
        payload.update(value)
    scalar_fields = (
        "project_id",
        "question",
        "sample",
        "date_range",
        "rule",
        "information_set",
        "return_denominator",
        "weighting",
        "aggregation",
        "accumulation",
        "data_version",
        "outcome",
        "status",
        "evidence_level",
        "future_impact",
        "session_id",
    )
    for field in scalar_fields:
        value = getattr(args, field, None)
        if value is not None:
            payload[field] = value
    list_fields = ("dataset_ids", "evidence", "limitations", "supersedes")
    for field in list_fields:
        value = getattr(args, field, None)
        if value:
            payload[field] = value
    if args.parameter:
        payload["parameters"] = {
            **dict(payload.get("parameters", {}) or {}),
            **parse_key_values(args.parameter, "parameter"),
        }
    return payload


def command_record_conclusion(args: argparse.Namespace) -> int:
    try:
        result = record_conclusion(args.root, args.workstream_id, conclusion_payload(args))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


def command_promote(args: argparse.Namespace) -> int:
    try:
        artifacts = {
            key: str(value)
            for key, value in parse_key_values(args.artifact, "artifact").items()
        }
        result = promote_conclusion(
            args.root,
            args.workstream_id,
            args.conclusion_id,
            intent=args.intent,
            decisions=args.decision or [],
            open_loops=args.open_loop or [],
            resolved_loops=args.resolve_loop or [],
            artifacts=artifacts,
        )
    except (OSError, ValueError) as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


def command_conclusions(args: argparse.Namespace) -> int:
    registry = load_registry(args.root)
    if args.workstream_id:
        workstream_ids = [args.workstream_id]
    elif args.project_id:
        workstream_ids = [
            workstream_id
            for workstream_id, meta in (registry.get("workstreams", {}) or {}).items()
            if args.project_id == meta.get("primary_project_id")
            or args.project_id in (meta.get("related_project_ids", []) or [])
        ]
    else:
        workstream_ids = [path.parent.name for path in (args.root / "workstreams").glob("*/context.yaml")]
    rows: list[dict[str, Any]] = []
    for workstream_id in workstream_ids:
        try:
            current = [
                {key: value for key, value in row.items() if key != "_line"}
                for row in read_conclusions(args.root, workstream_id)
            ]
        except ValueError as exc:
            print(f"ERROR {exc}", file=sys.stderr)
            return 2
        rows.extend(active_conclusions(current) if args.active_only else effective_conclusions(current))
    if args.id:
        rows = [row for row in rows if row.get("conclusion_id") == args.id]
    if args.query:
        terms = semantic_terms(args.query)
        rows = [
            row
            for row in rows
            if terms
            & semantic_terms(
                "\n".join(
                    str(row.get(key, ""))
                    for key in (
                        "question",
                        "outcome",
                        "rule",
                        "sample",
                        "return_denominator",
                        "semantic_key",
                    )
                )
            )
        ]
    def rank(row: dict[str, Any]) -> tuple[int, str]:
        overlap = len(semantic_terms(args.query or "") & semantic_terms(json.dumps(row, ensure_ascii=False)))
        return overlap, str(row.get("created_at", ""))
    rows.sort(key=rank, reverse=True)
    if not args.full:
        fields = ("conclusion_id", "workstream_id", "question", "outcome", "sample", "date_range",
                  "return_denominator", "evidence_level", "validity", "superseded", "limitations",
                  "validity_events", "unresolved_validity_event_ids", "evidence")
        rows = [{key: row[key] for key in fields if key in row} for row in rows]
    print(json.dumps(rows[: args.limit], ensure_ascii=False, indent=2, default=str))
    return 0


def json_input(path: str) -> dict[str, Any]:
    raw = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8-sig")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("input must be a JSON object")
    return value


def command_memory(args: argparse.Namespace) -> int:
    from workstream_memory import archive_update, closeout, continuation_context, list_experiments, query_rules
    if args.command == "context":
        result = continuation_context(args.root, args.workstream_id, query=args.query,
                                      experiment_id=args.experiment, limit=args.limit)
    elif args.command == "experiments":
        result = list_experiments(args.root, args.workstream_id, query=args.query,
                                  experiment_id=args.id, full=args.full, limit=args.limit)
    elif args.command == "rules":
        result = query_rules(args.root, args.workstream_id, query=args.query,
                             rule_id=args.id, full=args.full, limit=args.limit)
    else:
        operation = {"archive": archive_update, "closeout": closeout, "record-validity": record_validity}[args.command]
        result = operation(args.root, args.workstream_id, json_input(args.input))
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


def command_dashboard(args: argparse.Namespace) -> int:
    rendered = compile_dashboard(args.root)
    output = args.output
    if args.write_default:
        configured = (load_registry(args.root).get("workspace", {}) or {}).get(
            "generated_dashboard"
        )
        if not configured:
            print("ERROR registry has no workspace.generated_dashboard", file=sys.stderr)
            return 2
        output = Path(configured)
    if output:
        output = Path(output)
        atomic_text(output, rendered)
        print(json.dumps({"dashboard": str(output), "written": True}, ensure_ascii=False, indent=2))
    else:
        print(rendered)
    return 0


def command_close(args: argparse.Namespace) -> int:
    path = context_path(args.root, args.workstream_id)
    context = load_yaml(path)
    runtime_file = runtime_path(args.root, args.workstream_id)
    runtime = load_json(runtime_file, {})
    final_runtime = {
        key: value for key, value in runtime.items() if key not in VOLATILE_FINAL_KEYS
    }
    closed_at = now_iso()
    snapshot = {
        "schema_version": 1,
        "workstream_id": args.workstream_id,
        "closed_at": closed_at,
        "summary": args.summary.strip(),
        "final_audit": args.audit.strip(),
        "context": context,
        "final_runtime": final_runtime,
    }
    date_name = datetime.now().astimezone().strftime("%Y-%m-%d")
    history_path = (
        args.root / "history" / args.workstream_id / f"{date_name}-final.yaml"
    )
    if history_path.exists() and not args.replace:
        print(f"ERROR snapshot exists: {history_path}; use --replace", file=sys.stderr)
        return 2
    dump_yaml(history_path, snapshot)
    context["status"] = "closed"
    context["current_intent"] = args.summary.strip()
    context["open_loops"] = []
    context["updated_at"] = closed_at
    context["state_revision"] = int(context.get("state_revision", 0)) + 1
    dump_yaml(path, context)
    write_active_ids(
        args.root, [item for item in active_ids(args.root) if item != args.workstream_id]
    )
    if runtime_file.exists():
        runtime_file.unlink()
    print(
        json.dumps(
            {"closed": args.workstream_id, "snapshot": str(history_path)},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return None


def command_clean_runtime(args: argparse.Namespace) -> int:
    runtime_dir = args.root / "runtime"
    threshold = datetime.now().astimezone() - timedelta(hours=args.stale_hours)
    candidates = []
    for path in sorted(runtime_dir.glob("*.json")):
        if path.name == "active.json":
            continue
        workstream_id = path.stem
        runtime = load_json(path, {})
        verified = parse_time(runtime.get("verified_at"))
        context_file = context_path(args.root, workstream_id)
        context_status = load_yaml(context_file).get("status") if context_file.exists() else None
        alive = pid_alive(runtime.get("pid"))
        stale = verified is None or verified < threshold
        if context_status == "closed" or (stale and alive is False):
            candidates.append(
                {
                    "path": str(path),
                    "reason": "closed" if context_status == "closed" else "stale-dead",
                }
            )
    if args.apply:
        for item in candidates:
            Path(item["path"]).unlink(missing_ok=True)
    print(
        json.dumps(
            {"apply": args.apply, "candidates": candidates},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage compact Codex workstream state")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list")
    list_parser.set_defaults(func=command_list)

    resolve = subparsers.add_parser("resolve")
    resolve.add_argument("query", nargs="?", default="")
    resolve.add_argument("--limit", type=int, default=3)
    resolve.add_argument("--include-closed", action="store_true")
    resolve.add_argument("--cwd", default=str(Path.cwd()))
    resolve.set_defaults(func=command_resolve)

    audit = subparsers.add_parser("audit")
    audit.add_argument("workstream_id")
    audit.set_defaults(func=command_audit)

    status = subparsers.add_parser("status")
    status.add_argument("workstream_id")
    status.add_argument("--probe-pid", action="store_true")
    status.set_defaults(func=command_status)

    checkpoint = subparsers.add_parser("checkpoint")
    checkpoint.add_argument("workstream_id")
    checkpoint.add_argument("--stage")
    checkpoint.add_argument("--completed", type=int)
    checkpoint.add_argument("--total", type=int)
    checkpoint.add_argument("--errors", type=int)
    checkpoint.add_argument("--pid", type=int)
    checkpoint.add_argument("--eta")
    checkpoint.add_argument("--note")
    checkpoint.set_defaults(func=command_checkpoint)

    set_active = subparsers.add_parser("set-active")
    set_active.add_argument("workstream_id")
    set_active.set_defaults(func=command_set_active)

    record = subparsers.add_parser("record-conclusion")
    record.add_argument("workstream_id")
    record.add_argument("--input", help="JSON file path, or - for stdin")
    record.add_argument("--project-id")
    record.add_argument("--question")
    record.add_argument("--sample")
    record.add_argument("--date-range")
    record.add_argument("--rule")
    record.add_argument("--information-set")
    record.add_argument("--return-denominator")
    record.add_argument("--weighting")
    record.add_argument("--aggregation")
    record.add_argument("--accumulation")
    record.add_argument("--data-version")
    record.add_argument("--outcome")
    record.add_argument("--status", choices=["active", "blocked", "invalidated"])
    record.add_argument(
        "--evidence-level",
        choices=["exploratory", "diagnostic", "validated", "production"],
    )
    record.add_argument(
        "--future-impact", choices=["none", "changes_future_work"]
    )
    record.add_argument("--session-id")
    record.add_argument("--dataset-id", dest="dataset_ids", action="append")
    record.add_argument("--evidence", action="append")
    record.add_argument("--limitation", dest="limitations", action="append")
    record.add_argument("--supersedes", action="append")
    record.add_argument("--parameter", action="append", help="KEY=JSON_VALUE")
    record.set_defaults(func=command_record_conclusion)

    promote = subparsers.add_parser("promote")
    promote.add_argument("workstream_id")
    promote.add_argument("conclusion_id")
    promote.add_argument("--intent")
    promote.add_argument("--decision", action="append")
    promote.add_argument("--open-loop", action="append")
    promote.add_argument("--resolve-loop", action="append")
    promote.add_argument("--artifact", action="append", help="KEY=PATH")
    promote.set_defaults(func=command_promote)

    conclusions = subparsers.add_parser("conclusions")
    conclusions.add_argument("workstream_id", nargs="?")
    conclusions.add_argument("--project-id")
    conclusions.add_argument("--query")
    conclusions.add_argument("--id", help="Exact conclusion ID")
    conclusions.add_argument("--full", action="store_true", help="Include the full historical record and current validity")
    conclusions.add_argument("--active-only", action="store_true")
    conclusions.add_argument("--limit", type=int, default=5)
    conclusions.set_defaults(func=command_conclusions)

    context = subparsers.add_parser("context", help="Read current state and relevant experiment/rule cards")
    context.add_argument("workstream_id")
    context.add_argument("--query", default="")
    context.add_argument("--experiment")
    context.add_argument("--limit", type=int, default=3)
    context.set_defaults(func=command_memory)
    for name in ("experiments", "rules"):
        lookup = subparsers.add_parser(name)
        lookup.add_argument("workstream_id")
        lookup.add_argument("--query", default="")
        lookup.add_argument("--id")
        lookup.add_argument("--full", action="store_true")
        lookup.add_argument("--limit", type=int, default=3)
        lookup.set_defaults(func=command_memory)
    for name in ("archive", "closeout", "record-validity"):
        mutation = subparsers.add_parser(name)
        mutation.add_argument("workstream_id")
        mutation.add_argument("--input", required=True, help="JSON file path, or - for stdin")
        mutation.set_defaults(func=command_memory)

    dashboard = subparsers.add_parser("dashboard")
    dashboard.add_argument("--output", type=Path)
    dashboard.add_argument("--write-default", action="store_true")
    dashboard.set_defaults(func=command_dashboard)

    close = subparsers.add_parser("close")
    close.add_argument("workstream_id")
    close.add_argument("--summary", required=True)
    close.add_argument("--audit", required=True)
    close.add_argument("--replace", action="store_true")
    close.set_defaults(func=command_close)

    clean_runtime = subparsers.add_parser("clean-runtime")
    clean_runtime.add_argument("--stale-hours", type=float, default=72)
    clean_runtime.add_argument("--apply", action="store_true")
    clean_runtime.set_defaults(func=command_clean_runtime)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    args.root = args.root.resolve()
    try:
        if getattr(args, "limit", 1) < 1:
            raise ValueError("limit must be positive")
        if args.command == "close":
            with workstream_lock(args.root, args.workstream_id):
                return int(args.func(args))
        return int(args.func(args))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
