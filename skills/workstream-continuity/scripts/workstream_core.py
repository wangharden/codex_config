from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import time
import unicodedata
from contextlib import contextmanager
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any, Iterable

import yaml


SEMANTIC_FIELDS = (
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
)
EVIDENCE_LEVELS = {"exploratory", "diagnostic", "validated", "production"}
CONCLUSION_STATUSES = {"active", "blocked", "invalidated"}
FUTURE_IMPACTS = {"none", "changes_future_work"}
VALIDITY_ACTIONS = {"withdraw", "restrict", "review", "restore"}
_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[str, dict[str, Any]] = {}


@contextmanager
def workstream_lock(root: Path, workstream_id: str, timeout: float = 60.0):
    """Serialize complete read/modify/write operations, including nested callers.

    The OS releases the byte lock if a process exits. The small runtime lock file
    is deliberately not unlinked on release: deleting it would let another process
    lock a different file while a previous waiter still owns the old file handle.
    """
    root = Path(root).resolve()
    stream_path = (root / "workstreams" / workstream_id).resolve()
    if stream_path.parent != root / "workstreams":
        raise ValueError("workstream_id must name one workstream directory")
    key = os.path.normcase(str(stream_path))
    with _LOCKS_GUARD:
        entry = _LOCKS.setdefault(key, {"lock": threading.RLock(), "depth": 0})
    started = time.monotonic()
    if not entry["lock"].acquire(timeout=max(0.0, timeout)):
        raise TimeoutError(f"workstream merge lock is busy: {workstream_id}")
    handle = None
    acquired = False
    try:
        if entry["depth"]:
            entry["depth"] += 1
            try:
                yield
            finally:
                entry["depth"] -= 1
            return
        lock_path = root / "runtime" / "locks" / (hashlib.sha256(key.encode()).hexdigest()[:24] + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+b")
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"0")
            handle.flush()
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                if time.monotonic() - started >= timeout:
                    raise TimeoutError(f"workstream merge lock is busy: {workstream_id}") from None
                time.sleep(0.05)
        entry["depth"] = 1
        try:
            yield
        finally:
            entry["depth"] = 0
    finally:
        try:
            if acquired and handle is not None:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            if handle is not None:
                handle.close()
            entry["lock"].release()


def _serialized_workstream(function):
    @wraps(function)
    def locked(root: Path, workstream_id: str, *args, **kwargs):
        with workstream_lock(root, workstream_id):
            return function(root, workstream_id, *args, **kwargs)

    return locked


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


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


def load_yaml_mapping(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8-sig") as handle:
        value = yaml.safe_load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a mapping")
    return value


def context_path(root: Path, workstream_id: str) -> Path:
    return root / "workstreams" / workstream_id / "context.yaml"


def conclusions_path(root: Path, workstream_id: str) -> Path:
    return root / "workstreams" / workstream_id / "conclusions.jsonl"


def registry_path(root: Path) -> Path:
    return root / "registry.yaml"


def load_registry(root: Path) -> dict[str, Any]:
    path = registry_path(root)
    if not path.exists():
        return {"schema_version": 1, "workspace": {}, "projects": {}, "workstreams": {}}
    registry = load_yaml_mapping(path)
    registry.setdefault("workspace", {})
    registry.setdefault("projects", {})
    registry.setdefault("workstreams", {})
    return registry


def catalog_path(root: Path, registry: dict[str, Any] | None = None) -> Path:
    registry = registry or load_registry(root)
    configured = (registry.get("workspace", {}) or {}).get("data_catalog")
    return Path(configured) if configured else Path(r"E:\data\catalog\datasets.yaml")


def dashboard_path(root: Path, registry: dict[str, Any] | None = None) -> Path | None:
    registry = registry or load_registry(root)
    configured = (registry.get("workspace", {}) or {}).get("generated_dashboard")
    return Path(configured) if configured else None


def load_catalog(root: Path, registry: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    path = catalog_path(root, registry)
    if not path.exists():
        return {}
    value = load_yaml_mapping(path)
    datasets = value.get("datasets", [])
    if isinstance(datasets, dict):
        rows = []
        for dataset_id, payload in datasets.items():
            item = dict(payload or {})
            item.setdefault("dataset_id", dataset_id)
            rows.append(item)
    elif isinstance(datasets, list):
        rows = datasets
    else:
        raise ValueError(f"{path}: datasets must be a list or mapping")
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or not row.get("dataset_id"):
            raise ValueError(f"{path}: every dataset must have dataset_id")
        dataset_id = str(row["dataset_id"])
        if dataset_id in result:
            raise ValueError(f"{path}: duplicate dataset_id {dataset_id}")
        result[dataset_id] = row
    return result


def normalize_text(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value)).split())


def normalize_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): normalize_json(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, list):
        return [normalize_json(item) for item in value]
    if isinstance(value, tuple):
        return [normalize_json(item) for item in value]
    if isinstance(value, str):
        return normalize_text(value)
    return value


def stable_hash(value: Any) -> str:
    encoded = json.dumps(
        normalize_json(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _string_list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    result = [normalize_text(item) for item in value]
    if any(not item for item in result):
        raise ValueError(f"{field} contains blank items")
    return list(dict.fromkeys(result))


def semantic_projection(payload: dict[str, Any], catalog: dict[str, dict[str, Any]]) -> dict[str, Any]:
    projection: dict[str, Any] = {}
    for field in SEMANTIC_FIELDS:
        value = normalize_text(payload.get(field, ""))
        if not value:
            raise ValueError(f"missing semantic field: {field}")
        projection[field] = value
    dataset_ids = sorted(_string_list(payload.get("dataset_ids", []), "dataset_ids"))
    unknown = [dataset_id for dataset_id in dataset_ids if dataset_id not in catalog]
    if unknown:
        raise ValueError(f"unknown dataset_ids: {', '.join(unknown)}")
    projection["dataset_ids"] = dataset_ids
    frozen_versions = payload.get("dataset_versions")
    if frozen_versions is not None:
        if not isinstance(frozen_versions, dict):
            raise ValueError("dataset_versions must be a mapping")
        if set(frozen_versions) != set(dataset_ids):
            raise ValueError("dataset_versions keys must equal dataset_ids")
        projection["dataset_versions"] = normalize_json(frozen_versions)
    else:
        projection["dataset_versions"] = {
            dataset_id: {
                "canonical_path": catalog[dataset_id].get("canonical_path"),
                "last_verified": catalog[dataset_id].get("last_verified"),
            }
            for dataset_id in dataset_ids
        }
    parameters = payload.get("parameters", {}) or {}
    if not isinstance(parameters, dict):
        raise ValueError("parameters must be a mapping")
    projection["parameters"] = normalize_json(parameters)
    return projection


def semantic_key(payload: dict[str, Any], catalog: dict[str, dict[str, Any]]) -> str:
    return stable_hash(semantic_projection(payload, catalog))


def record_identity(record: dict[str, Any]) -> dict[str, Any]:
    excluded = {"conclusion_id", "created_at", "session_id"}
    return {key: value for key, value in record.items() if key not in excluded}


def expected_conclusion_id(record: dict[str, Any]) -> str:
    return f"c-{str(record['semantic_key'])[:12]}-{stable_hash(record_identity(record))[:12]}"


def read_conclusions(root: Path, workstream_id: str) -> list[dict[str, Any]]:
    path = conclusions_path(root, workstream_id)
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number}: record must be an object")
            value["_line"] = line_number
            records.append(value)
    return records


def is_validity_event(record: dict[str, Any]) -> bool:
    return record.get("record_type") == "validity_event"


def ledger_record_id(record: dict[str, Any]) -> str:
    return str(record.get("event_id" if is_validity_event(record) else "conclusion_id", ""))


def expected_validity_event_id(record: dict[str, Any]) -> str:
    identity = {
        key: value for key, value in record.items()
        if key not in {"event_id", "created_at", "session_id", "_line"}
    }
    return "v-" + stable_hash(identity)[:24]


def effective_conclusions(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project immutable conclusions and validity events without mutating either.

    Restrictions are scoped text assertions, not recomputed sub-sample results.
    The conservative summary status never authorizes use outside those scopes.
    """
    rows = list(records)
    conclusions = [row for row in rows if not is_validity_event(row)]
    superseded = {
        conclusion_id
        for row in conclusions
        for conclusion_id in (row.get("supersedes", []) or [])
    }
    events_by_target: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if is_validity_event(row):
            events_by_target.setdefault(str(row.get("target_conclusion_id", "")), []).append(row)
    result = []
    for original in conclusions:
        row = dict(original)
        conclusion_id = str(row.get("conclusion_id", ""))
        unresolved: dict[str, dict[str, Any]] = {}
        changes = []
        for event in events_by_target.get(conclusion_id, []):
            changes.append({
                key: event.get(key) for key in (
                    "event_id", "action", "scope", "reason", "evidence",
                    "resolves_event_ids", "replacement_pointer", "created_at",
                ) if key in event
            })
            event_id = str(event.get("event_id", ""))
            if event.get("action") == "restore":
                # Malformed restores must never silently turn an invalid result green.
                for resolved_id in event.get("resolves_event_ids", []) or []:
                    previous = unresolved.get(str(resolved_id))
                    if previous and event.get("evidence") and event.get("scope") == previous.get("scope"):
                        unresolved.pop(str(resolved_id))
            elif event.get("action") in {"withdraw", "restrict", "review"}:
                unresolved[event_id] = event
        actions = {event.get("action") for event in unresolved.values()}
        if row.get("status") == "invalidated" or "withdraw" in actions:
            validity = "withdrawn"
        elif row.get("status") == "blocked" or "review" in actions:
            validity = "needs_review"
        elif "restrict" in actions:
            validity = "restricted"
        else:
            validity = "valid"
        row.update({
            "superseded": conclusion_id in superseded,
            "validity": validity,
            "validity_events": changes,
            "unresolved_validity_event_ids": list(unresolved),
            "validity_fingerprint": stable_hash({
                "conclusion_id": conclusion_id,
                "validity": validity,
                "superseded": conclusion_id in superseded,
                "unresolved_event_ids": sorted(unresolved),
            }),
        })
        result.append(row)
    return result


def active_conclusions(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        row for row in effective_conclusions(records)
        if not row["superseded"] and row["validity"] != "withdrawn"
    ]


def _build_validity_record(
    workstream_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    action = normalize_text(payload.get("action", ""))
    if action not in VALIDITY_ACTIONS:
        raise ValueError(f"action must be one of {sorted(VALIDITY_ACTIONS)}")
    record: dict[str, Any] = {
        "schema_version": 1,
        "record_type": "validity_event",
        "workstream_id": workstream_id,
        "action": action,
    }
    for field in ("target_conclusion_id", "scope", "reason"):
        if not isinstance(payload.get(field), str) or not normalize_text(payload[field]):
            raise ValueError(f"{field} must be a nonblank string")
        record[field] = normalize_text(payload[field])
    record["evidence"] = _string_list(payload.get("evidence", []), "evidence")
    if not record["evidence"]:
        raise ValueError("at least one evidence pointer is required for validity changes")
    record["resolves_event_ids"] = sorted(_string_list(payload.get("resolves_event_ids", []), "resolves_event_ids"))
    if action == "restore" and not record["resolves_event_ids"]:
        raise ValueError("restore requires resolves_event_ids and new verification evidence")
    if action != "restore" and record["resolves_event_ids"]:
        raise ValueError("only restore may specify resolves_event_ids")
    if payload.get("replacement_pointer"):
        record["replacement_pointer"] = normalize_text(payload["replacement_pointer"])
    if "occurrence_id" in payload:
        if not isinstance(payload["occurrence_id"], str) or not normalize_text(payload["occurrence_id"]):
            raise ValueError("occurrence_id must be a nonblank string when supplied")
        record["occurrence_id"] = normalize_text(payload["occurrence_id"])
    record["session_id"] = normalize_text(payload.get("session_id", "")) or None
    record["created_at"] = normalize_text(payload.get("created_at", "")) or now_iso()
    record["event_id"] = expected_validity_event_id(record)
    return record


def _validate_validity_references(record: dict[str, Any], preceding: list[dict[str, Any]]) -> None:
    target_id = record["target_conclusion_id"]
    target = next((row for row in effective_conclusions(preceding) if row.get("conclusion_id") == target_id), None)
    if target is None:
        raise ValueError(f"unknown target_conclusion_id: {target_id}")
    if record["action"] != "restore":
        return
    unresolved = set(target["unresolved_validity_event_ids"])
    events = {row.get("event_id"): row for row in preceding if is_validity_event(row)}
    for event_id in record["resolves_event_ids"]:
        previous = events.get(event_id)
        if previous is None or previous.get("target_conclusion_id") != target_id:
            raise ValueError(f"restore references unknown event or different target: {event_id}")
        if event_id not in unresolved:
            raise ValueError(f"restore references an event that is not unresolved: {event_id}")
        if record["scope"] != previous.get("scope"):
            raise ValueError(f"restore scope must match the event being resolved: {event_id}")


@_serialized_workstream
def record_validity(root: Path, workstream_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not context_path(root, workstream_id).exists():
        raise ValueError(f"unknown workstream: {workstream_id}")
    record = _build_validity_record(workstream_id, payload)
    rows = read_conclusions(root, workstream_id)
    existing = next((row for row in rows if is_validity_event(row) and row.get("event_id") == record["event_id"]), None)
    if existing is not None:
        return {"action": "idempotent", "record": {key: value for key, value in existing.items() if key != "_line"}, "sync_required": True}
    _validate_validity_references(record, rows)
    if record["action"] == "restore":
        projected = active_conclusions([*rows, record])
        target = next((row for row in projected if row.get("conclusion_id") == record["target_conclusion_id"]), None)
        if target and any(row.get("semantic_key") == target.get("semantic_key") and row.get("conclusion_id") != target.get("conclusion_id") for row in projected):
            raise ValueError("restore would create conflicting active conclusions for the same semantic_key; resolve the explicit replacement first")
    _append_ledger_record(root, workstream_id, record)
    return {"action": "appended", "record": record, "sync_required": True}


def _append_ledger_record(root: Path, workstream_id: str, record: dict[str, Any]) -> None:
    path = conclusions_path(root, workstream_id)
    existing = path.read_text(encoding="utf-8-sig") if path.exists() else ""
    if existing and not existing.endswith("\n"):
        existing += "\n"
    atomic_text(path, existing + json.dumps(record, ensure_ascii=False) + "\n")


def _project_id(root: Path, workstream_id: str, payload: dict[str, Any]) -> str | None:
    if payload.get("project_id"):
        return normalize_text(payload["project_id"])
    registry = load_registry(root)
    meta = (registry.get("workstreams", {}) or {}).get(workstream_id, {}) or {}
    if meta.get("primary_project_id"):
        return str(meta["primary_project_id"])
    context_file = context_path(root, workstream_id)
    if context_file.exists():
        return load_yaml_mapping(context_file).get("project_id")
    return None


def build_conclusion_record(
    root: Path,
    workstream_id: str,
    payload: dict[str, Any],
    catalog: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    catalog = catalog if catalog is not None else load_catalog(root)
    if not context_path(root, workstream_id).exists():
        raise ValueError(f"unknown workstream: {workstream_id}")
    outcome = normalize_text(payload.get("outcome", ""))
    if not outcome:
        raise ValueError("outcome is required")
    evidence = _string_list(payload.get("evidence", []), "evidence")
    if not evidence:
        raise ValueError("at least one evidence pointer is required")
    evidence_level = normalize_text(payload.get("evidence_level", "diagnostic"))
    if evidence_level not in EVIDENCE_LEVELS:
        raise ValueError(f"evidence_level must be one of {sorted(EVIDENCE_LEVELS)}")
    status = normalize_text(payload.get("status", "active"))
    if status not in CONCLUSION_STATUSES:
        raise ValueError(f"status must be one of {sorted(CONCLUSION_STATUSES)}")
    future_impact = normalize_text(payload.get("future_impact", "none"))
    if future_impact not in FUTURE_IMPACTS:
        raise ValueError(f"future_impact must be one of {sorted(FUTURE_IMPACTS)}")
    projection = semantic_projection(payload, catalog)
    record: dict[str, Any] = {
        "schema_version": 1,
        "workstream_id": workstream_id,
        "project_id": _project_id(root, workstream_id, payload),
        **projection,
        "semantic_key": stable_hash(projection),
        "outcome": outcome,
        "status": status,
        "evidence_level": evidence_level,
        "future_impact": future_impact,
        "evidence": evidence,
        "limitations": _string_list(payload.get("limitations", []), "limitations"),
        "supersedes": _string_list(payload.get("supersedes", []), "supersedes"),
        "session_id": normalize_text(payload.get("session_id", "")) or None,
        "created_at": normalize_text(payload.get("created_at", "")) or now_iso(),
    }
    record["conclusion_id"] = expected_conclusion_id(record)
    return record


@_serialized_workstream
def record_conclusion(
    root: Path, workstream_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    catalog = load_catalog(root)
    record = build_conclusion_record(root, workstream_id, payload, catalog)
    rows = read_conclusions(root, workstream_id)
    clean_rows = [{key: value for key, value in row.items() if key != "_line"} for row in rows]
    by_id = {str(row.get("conclusion_id")): row for row in clean_rows if not is_validity_event(row)}
    if record["conclusion_id"] in by_id:
        return {
            "action": "idempotent",
            "record": by_id[record["conclusion_id"]],
            "sync_required": record["future_impact"] == "changes_future_work",
        }

    supersedes = set(record["supersedes"])
    unknown_supersedes = sorted(supersedes - set(by_id))
    if unknown_supersedes:
        raise ValueError(f"unknown supersedes ids: {', '.join(unknown_supersedes)}")
    for conclusion_id in supersedes:
        if by_id[conclusion_id].get("semantic_key") != record["semantic_key"]:
            raise ValueError("supersedes may only reference the same semantic_key")

    active_same_key = [
        row
        for row in active_conclusions(clean_rows)
        if row.get("semantic_key") == record["semantic_key"]
    ]
    active_ids = {str(row.get("conclusion_id")) for row in active_same_key}
    if active_ids and not active_ids.issubset(supersedes):
        raise ValueError(
            "conflicting active conclusion for the same semantic_key; "
            f"supersede: {', '.join(sorted(active_ids))}"
        )

    _append_ledger_record(root, workstream_id, record)
    return {
        "action": "appended",
        "record": record,
        "sync_required": record["future_impact"] == "changes_future_work",
    }


def _local_pointer(value: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:[\\/]", value) or value.startswith("\\\\"))


@_serialized_workstream
def audit_conclusions(root: Path, workstream_id: str) -> dict[str, Any]:
    """Check ledger/state consistency, not experimental or scientific validity."""
    errors: list[str] = []
    warnings: list[str] = []
    try:
        rows = read_conclusions(root, workstream_id)
    except ValueError as exc:
        return {
            "valid": False, "errors": [str(exc)], "warnings": [],
            "count": 0, "active_count": 0, "validity_event_count": 0,
            "pending_promotions": [], "pending_validity_events": [],
            "state_basis_status": "unreadable",
        }
    clean_rows = [{key: value for key, value in row.items() if key != "_line"} for row in rows]
    conclusion_rows = [row for row in clean_rows if not is_validity_event(row)]
    event_rows = [row for row in clean_rows if is_validity_event(row)]
    catalog = load_catalog(root)
    ids: dict[str, dict[str, Any]] = {}
    record_positions: dict[str, int] = {}
    valid_record_ids: set[str] = set()
    for position, (row, clean) in enumerate(zip(rows, clean_rows)):
        line = row.get("_line")
        record_id = ledger_record_id(clean)
        errors_before = len(errors)
        if not record_id:
            errors.append(f"line {line}: missing record identifier")
            continue
        if record_id in record_positions:
            errors.append(f"line {line}: duplicate record identifier {record_id}")
            valid_record_ids.discard(record_id)
        record_positions[record_id] = position
        if clean.get("workstream_id") != workstream_id:
            errors.append(f"line {line}: workstream_id mismatch")
        try:
            if is_validity_event(clean):
                normalized = _build_validity_record(workstream_id, clean)
                if record_id != expected_validity_event_id(clean) or record_id != normalized["event_id"]:
                    errors.append(f"line {line}: event_id mismatch")
                _validate_validity_references(normalized, clean_rows[:position])
            else:
                ids[record_id] = clean
                if clean.get("record_type") not in {None, "conclusion"}:
                    errors.append(f"line {line}: unknown record_type")
                if clean.get("semantic_key") != semantic_key(clean, catalog):
                    errors.append(f"line {line}: semantic_key mismatch")
                if record_id != expected_conclusion_id(clean):
                    errors.append(f"line {line}: conclusion_id mismatch")
                if clean.get("evidence_level") not in EVIDENCE_LEVELS:
                    errors.append(f"line {line}: invalid evidence_level")
                if clean.get("status") not in CONCLUSION_STATUSES:
                    errors.append(f"line {line}: invalid status")
                if clean.get("future_impact") not in FUTURE_IMPACTS:
                    errors.append(f"line {line}: invalid future_impact")
                for previous_id in clean.get("supersedes", []) or []:
                    previous = ids.get(str(previous_id))
                    if previous is None or str(previous_id) == record_id:
                        errors.append(f"{record_id}: supersedes unknown or nonpreceding id {previous_id}")
                    elif previous.get("semantic_key") != clean.get("semantic_key"):
                        errors.append(f"{record_id}: supersedes a different semantic_key")
        except (ValueError, TypeError, KeyError) as exc:
            errors.append(f"line {line}: {exc}")
        if len(errors) == errors_before:
            valid_record_ids.add(record_id)
        for pointer in clean.get("evidence", []) or []:
            # This checks the file, not the contents of a section locator.
            # Prefer a literal existing filename (which may itself contain #).
            target = str(pointer)
            if _local_pointer(target) and not (Path(target).exists() or ("#" in target and Path(target.split("#", 1)[0]).exists())):
                warnings.append(f"line {line}: evidence path is missing: {pointer}")

    effective = effective_conclusions(clean_rows)
    by_effective_id = {str(row.get("conclusion_id")): row for row in effective}
    active = [row for row in effective if not row["superseded"] and row["validity"] != "withdrawn"]
    active_by_key: dict[str, list[str]] = {}
    for row in active:
        active_by_key.setdefault(str(row.get("semantic_key")), []).append(str(row.get("conclusion_id")))
    for key, conclusion_ids in active_by_key.items():
        if len(conclusion_ids) > 1:
            errors.append(f"multiple active conclusions for semantic_key {key}: " + ", ".join(conclusion_ids))

    context = load_yaml_mapping(context_path(root, workstream_id))
    referenced_dataset_ids = {str(item) for item in context.get("dataset_ids", []) or []}
    referenced_dataset_ids.update(str(item) for row in conclusion_rows for item in row.get("dataset_ids", []) or [])
    for dataset_id in sorted(referenced_dataset_ids):
        item = catalog.get(dataset_id)
        if item is None:
            errors.append(f"context or conclusion references unknown dataset_id {dataset_id}")
            continue
        canonical = str(item.get("canonical_path", ""))
        external = bool(item.get("external")) or "://" in canonical or canonical.startswith("tdb:")
        if not external and canonical and not Path(canonical).exists():
            errors.append(f"referenced dataset is missing: {dataset_id} -> {canonical}")
        if str(item.get("preflight_status", "")).casefold() in {"blocked", "fail", "failed"}:
            errors.append(f"referenced dataset is preflight-blocked: {dataset_id}")
    promoted = set(context.get("current_conclusion_ids", []) or [])
    for conclusion_id in sorted(promoted):
        row = by_effective_id.get(conclusion_id)
        if row is None:
            errors.append(f"context references unknown conclusion_id {conclusion_id}")
        elif row["validity"] == "withdrawn":
            errors.append(f"context references withdrawn conclusion_id {conclusion_id}")
        elif row["superseded"]:
            errors.append(f"context references superseded conclusion_id {conclusion_id}")
        elif row["validity"] != "valid":
            warnings.append(f"context conclusion {conclusion_id} is {row['validity']}; its scope and evidence require review before use")

    basis = context.get("state_basis", {}) or {}
    if not isinstance(basis, dict):
        errors.append("state_basis must be a mapping")
        basis = {}
    reviewed_through = basis.get("ledger_reviewed_through")
    reviewed_position = -1
    if reviewed_through is None or reviewed_through == "":
        basis_status = "not_established"
        warnings.append("state basis is not established: no ledger_reviewed_through; historical changes have not been certified as reviewed")
    elif not isinstance(reviewed_through, str) or reviewed_through not in valid_record_ids:
        basis_status = "invalid"
        errors.append(f"state_basis.ledger_reviewed_through is not a valid ledger record id: {reviewed_through}")
    else:
        basis_status = "established"
        reviewed_position = record_positions[reviewed_through]
    if basis_status == "established":
        pending = [
            str(row.get("conclusion_id")) for position, row in enumerate(clean_rows)
            if position > reviewed_position and not is_validity_event(row)
            and row.get("future_impact") == "changes_future_work"
        ]
    else:
        # Preserve readable legacy state without silently certifying its ledger tail.
        pending = [str(row.get("conclusion_id")) for row in active
                   if row.get("future_impact") == "changes_future_work" and row.get("conclusion_id") not in promoted]
    pending_events = [
        str(row.get("event_id")) for position, row in enumerate(clean_rows)
        if position > reviewed_position and is_validity_event(row)
    ]
    if pending:
        errors.append("future-changing conclusions need state review: " + ", ".join(pending))
    if pending_events:
        errors.append("validity changes need state/dependency review: " + ", ".join(pending_events))
    return {
        "valid": not errors, "errors": errors, "warnings": warnings,
        "count": len(conclusion_rows), "active_count": len(active),
        "validity_event_count": len(event_rows),
        "pending_promotions": pending, "pending_validity_events": pending_events,
        "state_basis_status": basis_status,
        "ledger_reviewed_through": reviewed_through,
        "latest_conclusion": conclusion_rows[-1] if conclusion_rows else None,
        "latest_record_id": ledger_record_id(clean_rows[-1]) if clean_rows else None,
        "audit_scope": "ledger and current-state consistency; does not establish experimental validity",
    }


def _append_unique(values: list[Any], additions: Iterable[Any], limit: int, field: str = "state list") -> list[Any]:
    result = list(dict.fromkeys([*values, *additions]))
    if len(result) > limit:
        raise ValueError(
            f"{field} update would contain {len(result)} items (limit {limit}); "
            "nothing was removed or written. Review the affected state list and "
            "move historical material to its experiment card or rules before retrying."
        )
    return result


@_serialized_workstream
def promote_conclusion(
    root: Path,
    workstream_id: str,
    conclusion_id: str,
    *,
    intent: str | None = None,
    decisions: Iterable[str] = (),
    open_loops: Iterable[str] = (),
    resolved_loops: Iterable[str] = (),
    artifacts: dict[str, str] | None = None,
) -> dict[str, Any]:
    rows = [
        {key: value for key, value in row.items() if key != "_line"}
        for row in read_conclusions(root, workstream_id)
    ]
    effective = effective_conclusions(rows)
    selected = next((row for row in effective if row.get("conclusion_id") == conclusion_id), None)
    if selected is None:
        raise ValueError(f"unknown conclusion_id: {conclusion_id}")
    if selected.get("future_impact") != "changes_future_work":
        raise ValueError("only future-changing conclusions may be promoted")
    active_ids = {str(row.get("conclusion_id")) for row in active_conclusions(rows)}
    if conclusion_id not in active_ids:
        raise ValueError("cannot promote a superseded or withdrawn conclusion")
    if selected["validity"] != "valid":
        raise ValueError("cannot directly promote a restricted or needs-review conclusion; review its scope and evidence in the experiment card")

    path = context_path(root, workstream_id)
    context = load_yaml_mapping(path)
    before = json.dumps(context, ensure_ascii=False, sort_keys=True, default=str)
    current_ids = [
        str(item)
        for item in context.get("current_conclusion_ids", []) or []
        if str(item) in active_ids
    ]
    context["current_conclusion_ids"] = _append_unique(current_ids, [conclusion_id], 20, "current_conclusion_ids")
    context["dataset_ids"] = _append_unique(
        [str(item) for item in context.get("dataset_ids", []) or []],
        selected.get("dataset_ids", []) or [],
        40,
        "dataset_ids",
    )
    if selected.get("project_id") and not context.get("project_id"):
        context["project_id"] = selected["project_id"]
    if intent:
        context["current_intent"] = normalize_text(intent)
    context["frozen_decisions"] = _append_unique(
        list(context.get("frozen_decisions", []) or []),
        [normalize_text(item) for item in decisions if normalize_text(item)],
        30,
        "frozen_decisions",
    )
    current_open = list(context.get("open_loops", []) or [])
    resolved = {normalize_text(item) for item in resolved_loops if normalize_text(item)}
    current_open = [item for item in current_open if normalize_text(item) not in resolved]
    context["open_loops"] = _append_unique(
        current_open,
        [normalize_text(item) for item in open_loops if normalize_text(item)],
        20,
        "open_loops",
    )
    if artifacts:
        target = dict(context.get("artifacts", {}) or {})
        target.update({str(key): str(value) for key, value in artifacts.items()})
        if len(target) > 40:
            raise ValueError("promotion would exceed 40 artifact pointers")
        context["artifacts"] = target
    after = json.dumps(context, ensure_ascii=False, sort_keys=True, default=str)
    if before != after:
        context["state_revision"] = int(context.get("state_revision", 0)) + 1
        context["updated_at"] = now_iso()
        atomic_text(
            path,
            yaml.safe_dump(context, allow_unicode=True, sort_keys=False, width=100),
        )
    return {
        "workstream_id": workstream_id,
        "conclusion_id": conclusion_id,
        "promoted": True,
        "context_changed": before != after,
        "context_path": str(path),
    }


def workstream_metadata(root: Path, workstream_id: str) -> dict[str, Any]:
    registry = load_registry(root)
    meta = dict((registry.get("workstreams", {}) or {}).get(workstream_id, {}) or {})
    project_ids = []
    if meta.get("primary_project_id"):
        project_ids.append(str(meta["primary_project_id"]))
    project_ids.extend(str(item) for item in meta.get("related_project_ids", []) or [])
    projects = registry.get("projects", {}) or {}
    rows = read_conclusions(root, workstream_id)
    clean_rows = [{key: value for key, value in row.items() if key != "_line"} for row in rows]
    active = active_conclusions(clean_rows)
    conclusion_rows = [row for row in clean_rows if not is_validity_event(row)]
    return {
        "primary_project_id": meta.get("primary_project_id"),
        "project_ids": list(dict.fromkeys(project_ids)),
        "projects": {
            project_id: projects.get(project_id, {})
            for project_id in dict.fromkeys(project_ids)
        },
        "conclusions_path": str(conclusions_path(root, workstream_id)),
        "conclusion_count": len(conclusion_rows),
        "active_conclusion_count": len(active),
        "validity_event_count": len(clean_rows) - len(conclusion_rows),
        "latest_conclusion_id": conclusion_rows[-1].get("conclusion_id") if conclusion_rows else None,
        "latest_record_id": ledger_record_id(clean_rows[-1]) if clean_rows else None,
        "data_catalog": str(catalog_path(root, registry)),
        "generated_dashboard": str(dashboard_path(root, registry) or ""),
    }


def registry_search_text(root: Path, workstream_id: str) -> str:
    registry = load_registry(root)
    meta = (registry.get("workstreams", {}) or {}).get(workstream_id, {}) or {}
    chunks: list[str] = []
    chunks.extend(str(item) for item in meta.get("aliases", []) or [])
    project_ids = [meta.get("primary_project_id"), *(meta.get("related_project_ids", []) or [])]
    for project_id in project_ids:
        project = (registry.get("projects", {}) or {}).get(project_id, {}) or {}
        chunks.extend(
            str(item)
            for item in [project_id, project.get("title"), *(project.get("aliases", []) or []), *(project.get("roots", []) or [])]
            if item
        )
    return "\n".join(chunks)


def _md(value: Any, limit: int | None = None) -> str:
    text = normalize_text(value).replace("|", "\\|")
    if limit and len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def compile_dashboard(root: Path) -> str:
    registry = load_registry(root)
    catalog = load_catalog(root, registry)
    dataset_rows = []
    for dataset_id, item in catalog.items():
        canonical = str(item.get("canonical_path", ""))
        external = bool(item.get("external")) or "://" in canonical or canonical.startswith("tdb:")
        if external:
            status = "external"
        elif str(item.get("preflight_status", "")).casefold() in {
            "blocked",
            "fail",
            "failed",
        }:
            status = "blocked"
        else:
            status = "available" if canonical and Path(canonical).exists() else "missing"
        dataset_rows.append((status, dataset_id, item, canonical))
    status_order = {"missing": 0, "blocked": 1, "external": 2, "available": 3}
    dataset_rows.sort(key=lambda row: (status_order[row[0]], row[1]))

    lines = [
        "# E盘量化研究数据看板（自动生成）",
        "",
        f"生成时间：{now_iso()}",
        "",
        "本页是编译视图。数据地址以 catalog 的 `dataset_id` 解析结果为准；结论以各 workstream 的 `conclusions.jsonl` 为准。请勿手工把本页当作新的真值源。",
        "",
        "## 数据目录",
        "",
        f"Catalog：`{catalog_path(root, registry)}`；注册项：{len(dataset_rows)}。",
        "",
        "| 状态 | dataset_id | 类型 | last_verified | canonical_path |",
        "|---|---|---|---|---|",
    ]
    for status, dataset_id, item, canonical in dataset_rows:
        lines.append(
            f"| {status} | `{_md(dataset_id)}` | {_md(item.get('kind', ''))} | "
            f"{_md(item.get('last_verified', ''))} | `{_md(canonical)}` |"
        )

    lines.extend(["", "## 项目与 Workstream", "", "| project | roots | workstreams |", "|---|---|---|"])
    projects = registry.get("projects", {}) or {}
    workstreams_meta = registry.get("workstreams", {}) or {}
    for project_id, project in sorted(projects.items()):
        linked = [
            workstream_id
            for workstream_id, meta in workstreams_meta.items()
            if project_id == meta.get("primary_project_id")
            or project_id in (meta.get("related_project_ids", []) or [])
        ]
        roots = "<br>".join(f"`{_md(item)}`" for item in project.get("roots", []) or [])
        lines.append(
            f"| `{_md(project_id)}` {_md(project.get('title', ''))} | {roots} | "
            + ", ".join(f"`{_md(item)}`" for item in linked)
            + " |"
        )

    all_active: list[dict[str, Any]] = []
    for context_file in sorted((root / "workstreams").glob("*/context.yaml")):
        workstream_id = context_file.parent.name
        rows = [
            {key: value for key, value in row.items() if key != "_line"}
            for row in read_conclusions(root, workstream_id)
        ]
        all_active.extend(active_conclusions(rows))
    all_active.sort(key=lambda row: str(row.get("created_at", "")), reverse=True)
    lines.extend(
        [
            "",
            "## 当前研究结论与适用状态",
            "",
            "不同 semantic_key 代表不同口径；已替代或撤回的结论不列入本视图。受限或待复核结论保留显示，不能作为无条件有效依据；静态检查不证明实验有效。",
            "",
            "| workstream | level | validity / limits | semantic_key | question | outcome |",
            "|---|---|---|---|---|---|",
        ]
    )
    for row in all_active[:100]:
        unresolved_ids = set(row.get("unresolved_validity_event_ids", []))
        limits = "; ".join(
            f"{event.get('scope', '')}: {event.get('reason', '')}"
            for event in row.get("validity_events", [])
            if event.get("event_id") in unresolved_ids
        )
        lines.append(
            f"| `{_md(row.get('workstream_id', ''))}` | {_md(row.get('evidence_level', ''))} | "
            f"{_md(row.get('validity', ''))}{': ' + _md(limits, 180) if limits else ''} | "
            f"`{_md(str(row.get('semantic_key', ''))[:12])}` | {_md(row.get('question', ''), 90)} | "
            f"{_md(row.get('outcome', ''), 140)} |"
        )
    if not all_active:
        lines.append("| — | — | — | — | 尚无结构化结论 | — |")
    lines.append("")
    return "\n".join(lines)
