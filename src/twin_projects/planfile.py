"""Read-only normalization of project Planfile documents for clients and APIs."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml

SCHEMA_ID = "twin-projects.project-plan/v1"
PLANFILE_PATHS = (
    Path("planfile.yaml"),
    Path("planfile.yml"),
    Path(".wellmanifest/planfile.yaml"),
    Path(".wellmanifest/planfile.yml"),
    Path("project/planfile-tickets.yaml"),
)
STATUS_ORDER = ("done", "in_progress", "needs_attention", "blocked", "planned")
STATUS_LABELS = {
    "done": "Wykonane",
    "in_progress": "W toku",
    "needs_attention": "Wymaga poprawy",
    "blocked": "Zablokowane",
    "planned": "Kolejne",
}
STATUS_ALIASES = {
    "accepted": "done",
    "complete": "done",
    "completed": "done",
    "done": "done",
    "passed": "done",
    "active": "in_progress",
    "doing": "in_progress",
    "in-progress": "in_progress",
    "in_progress": "in_progress",
    "started": "in_progress",
    "blocked": "blocked",
    "failed": "blocked",
    "needs-attention": "needs_attention",
    "needs-improvement": "needs_attention",
    "needs_attention": "needs_attention",
    "needs_improvement": "needs_attention",
    "requires-follow-up": "needs_attention",
    "requires_follow_up": "needs_attention",
    "next": "planned",
    "open": "planned",
    "pending": "planned",
    "planned": "planned",
    "todo": "planned",
}
DEFAULT_PROGRESS = {
    "done": 100,
    "in_progress": 50,
    "needs_attention": 25,
    "blocked": 0,
    "planned": 0,
}
PRIORITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}
PRIORITY_LABELS = {
    "critical": "krytyczny",
    "high": "wysoki",
    "medium": "średni",
    "low": "niski",
}
NEXT_STATUS_RANK = {
    "blocked": 0,
    "needs_attention": 1,
    "in_progress": 2,
    "planned": 3,
    "done": 4,
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _text(value: object, default: str = "") -> str:
    return str(value).strip() if value is not None else default


def _list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _status(item: dict[str, Any]) -> str:
    if item.get("done") is True or item.get("completed") is True:
        return "done"
    raw = _text(item.get("status"), "planned").casefold().replace(" ", "_")
    return STATUS_ALIASES.get(raw, "planned")


def _progress(item: dict[str, Any], status: str) -> int:
    value = item.get("progress", DEFAULT_PROGRESS[status])
    try:
        number = round(float(value))
    except (TypeError, ValueError):
        return DEFAULT_PROGRESS[status]
    return max(0, min(100, number))


def _strings(value: object) -> list[str]:
    return [_text(item) for item in _list(value) if _text(item)]


def _normalized_task(
    value: object, *, group_id: str, group_name: str, order: int
) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    title = _text(value.get("title") or value.get("name"))
    if not title:
        return None
    status = _status(value)
    identifier = _text(value.get("id") or value.get("dedupe_key"), f"task-{order + 1}")
    priority = _text(value.get("priority"), "medium").casefold()
    if priority not in PRIORITY_RANK:
        priority = "medium"
    return {
        "id": identifier,
        "title": title,
        "description": _text(value.get("description") or value.get("summary")),
        "status": status,
        "status_label": STATUS_LABELS[status],
        "progress": _progress(value, status),
        "priority": priority,
        "priority_label": PRIORITY_LABELS[priority],
        "estimate": _text(value.get("estimate")),
        "files": _strings(value.get("files")),
        "labels": _strings(value.get("labels") or value.get("views")),
        "evidence": _strings(value.get("evidence")),
        "next_action": _text(
            value.get("next_action") or value.get("remediation") or value.get("fix")
        ),
        "group_id": group_id,
        "group_name": group_name,
        "order": order,
    }


def _task_groups(document: dict[str, Any]) -> list[tuple[str, str, list[object]]]:
    groups: list[tuple[str, str, list[object]]] = []
    for collection_name in ("phases", "milestones", "sprints"):
        for index, group in enumerate(_list(document.get(collection_name))):
            if not isinstance(group, dict):
                continue
            tasks = group.get("tasks")
            if not isinstance(tasks, list):
                tasks = group.get("task_patterns")
            if not isinstance(tasks, list):
                tasks = group.get("tickets")
            if not isinstance(tasks, list):
                continue
            identifier = _text(group.get("id"), f"{collection_name}-{index + 1}")
            name = _text(group.get("name") or group.get("title"), identifier)
            groups.append((identifier, name, tasks))
    direct = document.get("tasks")
    if isinstance(direct, list):
        groups.append(("tasks", "Zadania", direct))
    tickets = document.get("tickets")
    if isinstance(tickets, list):
        groups.append(("tickets", "Zgłoszenia Planfile", tickets))
    return groups


def _empty(project_id: str, *, status: str, message: str) -> dict[str, object]:
    return {
        "schema_id": SCHEMA_ID,
        "project_id": project_id,
        "available": False,
        "status": status,
        "status_label": "Brak planu" if status == "missing" else "Plan nieczytelny",
        "message": message,
        "source": None,
        "progress_percent": 0,
        "counts": {key: 0 for key in STATUS_ORDER},
        "tasks": [],
        "groups": [],
        "board": [],
        "next_tasks": [],
    }


def load_project_plan(project_root: Path, project_id: str) -> dict[str, object]:
    """Load the first declared Planfile and expose a stable presentation model."""
    source = next(
        (
            project_root / rel
            for rel in PLANFILE_PATHS
            if (project_root / rel).is_file()
        ),
        None,
    )
    if source is None:
        return _empty(
            project_id,
            status="missing",
            message="Dodaj planfile.yaml, aby pokazać postęp i kolejne zadania.",
        )
    try:
        document = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        return _empty(
            project_id, status="invalid", message=f"Planfile jest nieczytelny: {exc}"
        )
    if not isinstance(document, dict):
        return _empty(
            project_id,
            status="invalid",
            message=(
                "Planfile musi zawierać obiekt z listą zadań, etapów albo sprintów."
            ),
        )

    tasks: list[dict[str, object]] = []
    declared_groups = _task_groups(document)
    for group_id, group_name, entries in declared_groups:
        for value in entries:
            task = _normalized_task(
                value, group_id=group_id, group_name=group_name, order=len(tasks)
            )
            if task is not None:
                tasks.append(task)

    counts = {key: 0 for key in STATUS_ORDER}
    for task in tasks:
        counts[str(task["status"])] += 1
    progress = (
        round(sum(int(task["progress"]) for task in tasks) / len(tasks)) if tasks else 0
    )
    if tasks and counts["done"] == len(tasks):
        overall = "done"
    elif counts["blocked"] or counts["needs_attention"]:
        overall = "needs_attention"
    elif counts["in_progress"] or counts["done"]:
        overall = "in_progress"
    else:
        overall = "planned"

    groups: list[dict[str, object]] = []
    for group_id, group_name, _entries in declared_groups:
        group_tasks = [task for task in tasks if task["group_id"] == group_id]
        if not group_tasks:
            continue
        groups.append(
            {
                "id": group_id,
                "name": group_name,
                "progress_percent": round(
                    sum(int(task["progress"]) for task in group_tasks)
                    / len(group_tasks)
                ),
                "done": sum(task["status"] == "done" for task in group_tasks),
                "total": len(group_tasks),
                "tasks": group_tasks,
            }
        )

    board_statuses = ("done", "in_progress", "needs_attention", "planned")
    board = []
    for status in board_statuses:
        column_tasks = [
            task
            for task in tasks
            if task["status"] == status
            or (status == "needs_attention" and task["status"] == "blocked")
        ]
        board.append(
            {
                "id": status,
                "label": STATUS_LABELS[status],
                "tasks": column_tasks,
                "count": len(column_tasks),
            }
        )
    next_tasks = sorted(
        (task for task in tasks if task["status"] != "done"),
        key=lambda task: (
            NEXT_STATUS_RANK[str(task["status"])],
            PRIORITY_RANK[str(task["priority"])],
            int(task["order"]),
        ),
    )[:5]
    relative = source.relative_to(project_root).as_posix()
    return {
        "schema_id": SCHEMA_ID,
        "project_id": project_id,
        "available": True,
        "status": overall,
        "status_label": STATUS_LABELS[overall],
        "message": _text(document.get("goal") or document.get("description")),
        "name": _text(
            document.get("name") or document.get("project_name"), "Plan projektu"
        ),
        "updated_at": _text(document.get("updated_at")),
        "source": {
            "path": relative,
            "sha256": _sha256(source),
        },
        "progress_percent": progress,
        "counts": counts,
        "tasks": tasks,
        "groups": groups,
        "board": board,
        "next_tasks": next_tasks,
    }
