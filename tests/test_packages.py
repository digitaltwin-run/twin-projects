from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from twin_projects import (
    APACHE_LICENSE_PATH,
    MANIFEST_RELATIVE,
    PROJECT_PACKAGE_SCHEMA_PATH,
    ProjectPackageError,
    ProjectPackageStore,
)


def store_at(tmp_path: Path) -> ProjectPackageStore:
    root = tmp_path / "artifacts"
    (root / ".wellmanifest").mkdir(parents=True)
    (root / MANIFEST_RELATIVE).write_text(
        json.dumps(
            {
                "schema_id": "wellmanifest.project-package/v1",
                "schema_version": "1.0.0",
                "project_id": "legacy",
                "name": "Legacy",
                "kind": "mixed",
                "root_mode": "legacy",
            }
        ),
        encoding="utf-8",
    )
    (root / "firmware").mkdir()
    (root / "firmware/code.py").write_text("PIN = 1\n", encoding="utf-8")
    return ProjectPackageStore(root)


def test_packaged_schema_and_apache_license_are_valid() -> None:
    schema = json.loads(PROJECT_PACKAGE_SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    assert (
        APACHE_LICENSE_PATH.read_text(encoding="utf-8")
        .lstrip()
        .startswith("Apache License")
    )


def test_create_isolated_project_with_plan_and_fixed_license(tmp_path: Path) -> None:
    store = store_at(tmp_path)

    created = store.create("Robot mobilny")
    root = store.project_root("robot-mobilny")

    assert created["root_mode"] == "managed"
    assert (root / "LICENSE").read_bytes() == APACHE_LICENSE_PATH.read_bytes()
    assert store.planfile("robot-mobilny")["counts"]["planned"] == 3
    assert (store.project_root("legacy") / "firmware/code.py").read_text() == (
        "PIN = 1\n"
    )


def test_deterministic_export_roundtrips_between_computers(tmp_path: Path) -> None:
    source = store_at(tmp_path / "pc1")
    source.create("Mysz RP2040", project_id="mysz-rp2040")
    source.upload("mysz-rp2040", "firmware/code.py", b"SENSOR_CS = 13\n")

    first = source.export_zip("mysz-rp2040")
    assert first == source.export_zip("mysz-rp2040")

    destination = store_at(tmp_path / "pc2")
    imported = destination.import_zip(
        "Mysz przeniesiona", first, project_id="mysz-przeniesiona"
    )
    assert (
        imported["content_fingerprint_sha256"]
        == source.describe("mysz-rp2040")["content_fingerprint_sha256"]
    )


def test_upload_never_overwrites_kicad_source(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    store.create("Elektronika")
    store.upload("elektronika", "pcb/main.kicad_sch", b"candidate source")
    current = next(
        item
        for item in store.files("elektronika")
        if item["path"] == "pcb/main.kicad_sch"
    )

    with pytest.raises(ProjectPackageError) as raised:
        store.upload(
            "elektronika",
            "pcb/main.kicad_sch",
            b"replacement",
            overwrite=True,
            expected_sha256=str(current["sha256"]),
        )

    assert raised.value.code == "PROJECT_EDA_CANDIDATE_REQUIRED"


def test_candidates_can_live_in_an_external_runtime_store(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    store.create("Elektronika")
    candidates_root = tmp_path / "runtime" / "kicad-edits"
    candidate_dir = candidates_root / "bootstrap" / "pcb"
    candidate_dir.mkdir(parents=True)
    candidate = candidate_dir / "main.kicad_sch"
    candidate.write_text("(kicad_sch)\n", encoding="utf-8")
    (candidate_dir / "change.json").write_text(
        json.dumps(
            {
                "project_id": "elektronika",
                "revision_id": "rev:bootstrap",
                "candidate_path": "bootstrap/pcb/main.kicad_sch",
                "source": {
                    "path": ".projects/elektronika/pcb/main.kicad_sch",
                    "exists": False,
                },
                "validation": {"status": "not_run"},
            }
        ),
        encoding="utf-8",
    )

    external = ProjectPackageStore(store.root, candidates_root=candidates_root)

    assert external.eda_candidates("elektronika") == [
        {
            "revision_id": "rev:bootstrap",
            "path": "bootstrap/pcb/main.kicad_sch",
            "name": "main.kicad_sch",
            "bytes": len("(kicad_sch)\n"),
            "sha256": (
                "fef5ea64221685b5520313c899e14864"
                "a1c1028dcdf43926a38e7f6a7a7de50a"
            ),
            "source_path": ".projects/elektronika/pcb/main.kicad_sch",
            "new_source": True,
            "created_at": None,
            "validation": {"status": "not_run"},
        }
    ]


def test_zip_import_rejects_path_escape(tmp_path: Path) -> None:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("../escape.py", b"bad")

    with pytest.raises(ProjectPackageError) as raised:
        store_at(tmp_path).import_zip("Unsafe", output.getvalue())

    assert raised.value.code == "PROJECT_PATH_INVALID"
