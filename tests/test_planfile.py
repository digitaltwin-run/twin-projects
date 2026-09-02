from __future__ import annotations

from pathlib import Path

from twin_projects import load_project_plan


def test_planfile_exposes_progress_board_and_next_work(tmp_path: Path) -> None:
    (tmp_path / "planfile.yaml").write_text(
        """name: Robot
phases:
- id: hardware
  name: Hardware
  tasks:
  - id: pcb
    title: PCB
    status: completed
  - id: drc
    title: DRC
    status: requires_follow_up
    progress: 60
    priority: critical
    next_action: Fix clearance.
  - id: fab
    title: Production
    status: planned
""",
        encoding="utf-8",
    )

    plan = load_project_plan(tmp_path, "robot")

    assert plan["status"] == "needs_attention"
    assert plan["progress_percent"] == 53
    assert [column["count"] for column in plan["board"]] == [1, 0, 1, 1]
    assert [task["id"] for task in plan["next_tasks"]] == ["drc", "fab"]
