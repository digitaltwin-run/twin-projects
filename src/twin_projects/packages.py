"""Framework-neutral isolated project packages.

The current artifacts directory remains the legacy project.  Additional
projects live below ``.projects/<project-id>`` so the existing EDA surface can
keep using its exact paths while project creation, import and consolidation
gain an explicit boundary.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import stat
import unicodedata
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal

from .planfile import load_project_plan

SCHEMA_ID = "wellmanifest.project-package/v1"
MANIFEST_RELATIVE = Path(".wellmanifest/project-package.json")
MANAGED_RELATIVE = Path(".projects")
DEFAULT_LICENSE = {"spdx": "Apache-2.0", "file": "LICENSE"}
APACHE_LICENSE_PATH = Path(__file__).parent / "data" / "LICENSE.Apache-2.0.txt"
PROJECT_PACKAGE_SCHEMA_PATH = (
    Path(__file__).parent / "data" / "project-package.schema.v1.json"
)
STANDARD_DIRECTORIES = ("pcb", "cad", "firmware", "software", "docs", "assets", "misc")
MAX_UPLOAD_BYTES = 128 * 1024 * 1024
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_FILES = 5_000
MAX_ARCHIVE_EXPANDED_BYTES = 512 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200
PROJECT_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
TRANSIENT_TOP_LEVEL = {
    ".projects",
    ".twinstudio",
    ".drc-scratch",
    ".repair-scratch",
    "eda-candidates",
}
TRANSIENT_DIRECTORY_NAMES = {
    ".git",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "node_modules",
    "venv",
}
TRANSIENT_FILE_SUFFIXES = {".pyc", ".pyo"}


class ProjectPackageError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return _sha256_bytes(encoded.encode("utf-8"))


def slugify(name: str) -> str:
    normalized = (
        unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    )
    value = re.sub(r"[^a-z0-9]+", "-", normalized.casefold()).strip("-")
    if not value:
        raise ProjectPackageError(
            "PROJECT_ID_INVALID", "Nazwa nie tworzy poprawnego identyfikatora projektu"
        )
    return value[:80].rstrip("-")


def classify_path(relative: PurePosixPath) -> str:
    suffix = relative.suffix.casefold()
    if suffix in {
        ".kicad_pcb",
        ".kicad_sch",
        ".kicad_pro",
        ".kicad_prl",
        ".kicad_wks",
        ".kicad_mod",
    }:
        return "pcb"
    if suffix in {".step", ".stp", ".stl", ".scad", ".dxf", ".glb", ".gltf", ".obj"}:
        return "cad"
    if suffix in {".py", ".ino", ".c", ".h", ".cpp", ".hpp", ".rs"}:
        lowered = "/".join(relative.parts).casefold()
        return (
            "firmware"
            if any(token in lowered for token in ("firmware", "hal", "circuitpython"))
            else "software"
        )
    if suffix in {".md", ".txt", ".rst", ".pdf", ".csv", ".tsv"}:
        return "docs"
    if suffix in {".svg", ".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        return "assets"
    return "misc"


def _safe_relative(
    value: str, *, allow_package_manifest: bool = False
) -> PurePosixPath:
    normalized = value.strip().replace("\\", "/")
    try:
        relative = PurePosixPath(normalized)
    except ValueError as exc:
        raise ProjectPackageError(
            "PROJECT_PATH_INVALID", "Nieprawidłowa ścieżka pliku"
        ) from exc
    if (
        not normalized
        or relative.is_absolute()
        or any(part in {"", ".", ".."} for part in relative.parts)
        or any("\x00" in part or ":" in part for part in relative.parts)
    ):
        raise ProjectPackageError(
            "PROJECT_PATH_INVALID",
            "Ścieżka musi być względna i nie może wychodzić poza projekt",
        )
    if relative.parts[0] in {".projects", ".twinstudio"} or (
        not allow_package_manifest
        and relative == PurePosixPath(MANIFEST_RELATIVE.as_posix())
    ):
        raise ProjectPackageError(
            "PROJECT_PATH_RESERVED", "Ta ścieżka należy do metadanych projektu"
        )
    return relative


def _segregated(relative: PurePosixPath) -> PurePosixPath:
    category = classify_path(relative)
    if relative.parts[0] in {*STANDARD_DIRECTORIES, ".wellmanifest"}:
        return relative
    return PurePosixPath(category, *relative.parts)


def _is_transient(relative: PurePosixPath) -> bool:
    return (
        any(part in TRANSIENT_DIRECTORY_NAMES for part in relative.parts)
        or relative.suffix.casefold() in TRANSIENT_FILE_SUFFIXES
    )


class ProjectPackageStore:
    def __init__(
        self,
        artifacts_root: Path,
        *,
        candidates_root: Path | None = None,
    ) -> None:
        self.root = artifacts_root.expanduser().resolve()
        self.managed_root = self.root / MANAGED_RELATIVE
        self.candidates_root = (
            candidates_root.expanduser().resolve()
            if candidates_root is not None
            else (self.root / "eda-candidates").resolve()
        )

    def _legacy_manifest(self) -> dict[str, object]:
        path = self.root / MANIFEST_RELATIVE
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        return {
            **payload,
            "schema_id": SCHEMA_ID,
            "schema_version": "1.0.0",
            "project_id": str(payload.get("project_id") or "klawiatura"),
            "name": str(payload.get("name") or "klawiatura"),
            "kind": str(payload.get("kind") or "mixed"),
            "root_mode": "legacy",
        }

    @property
    def legacy_id(self) -> str:
        return str(self._legacy_manifest()["project_id"])

    @staticmethod
    def _validate_id(project_id: str) -> str:
        value = project_id.strip()
        if not PROJECT_ID_PATTERN.fullmatch(value) or len(value) > 80:
            raise ProjectPackageError(
                "PROJECT_ID_INVALID",
                "Id projektu może zawierać małe litery, cyfry i myślniki",
            )
        return value

    def project_root(self, project_id: str) -> Path:
        value = self._validate_id(project_id)
        if value == self.legacy_id:
            return self.root
        path = (self.managed_root / value).resolve()
        if not path.is_relative_to(self.managed_root.resolve()) or not path.is_dir():
            raise ProjectPackageError(
                "PROJECT_NOT_FOUND", f"Nie znaleziono projektu: {value}"
            )
        if not (path / MANIFEST_RELATIVE).is_file():
            raise ProjectPackageError(
                "PROJECT_MANIFEST_MISSING", f"Projekt {value} nie ma manifestu paczki"
            )
        return path

    def _manifest(self, project_id: str) -> dict[str, object]:
        if project_id == self.legacy_id:
            return self._legacy_manifest()
        path = self.project_root(project_id) / MANIFEST_RELATIVE
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProjectPackageError(
                "PROJECT_MANIFEST_INVALID",
                f"Manifest projektu {project_id} jest nieczytelny",
            ) from exc
        if not isinstance(payload, dict) or payload.get("schema_id") != SCHEMA_ID:
            raise ProjectPackageError(
                "PROJECT_MANIFEST_INVALID",
                f"Manifest projektu {project_id} ma nieobsługiwany schemat",
            )
        if (
            payload.get("project_id") != project_id
            or payload.get("root_mode") != "managed"
        ):
            raise ProjectPackageError(
                "PROJECT_MANIFEST_INVALID",
                f"Manifest projektu {project_id} nie zgadza się z jego folderem",
            )
        return payload

    def _iter_files(self, project_id: str, *, include_manifest: bool = True):
        root = self.project_root(project_id)
        collected: list[tuple[Path, PurePosixPath]] = []
        for current, directories, filenames in os.walk(root, followlinks=False):
            current_path = Path(current)
            current_relative = current_path.relative_to(root)
            kept_directories: list[str] = []
            for directory in directories:
                candidate = current_path / directory
                top_level = (
                    directory
                    if current_relative == Path(".")
                    else current_relative.parts[0]
                )
                if (
                    candidate.is_symlink()
                    or directory == ".twinstudio"
                    or directory in TRANSIENT_DIRECTORY_NAMES
                ):
                    continue
                if project_id == self.legacy_id and top_level in TRANSIENT_TOP_LEVEL:
                    continue
                kept_directories.append(directory)
            directories[:] = kept_directories
            for filename in filenames:
                path = current_path / filename
                relative = path.relative_to(root)
                try:
                    if (
                        path.is_symlink()
                        or not path.is_file()
                        or path.name == ".gitkeep"
                        or path.suffix.casefold() in TRANSIENT_FILE_SUFFIXES
                    ):
                        continue
                except OSError:
                    continue
                if not include_manifest and relative == MANIFEST_RELATIVE:
                    continue
                collected.append((path, PurePosixPath(relative.as_posix())))
        yield from sorted(collected, key=lambda item: item[1].as_posix().casefold())

    def fingerprint(self, project_id: str) -> str:
        entries = [
            (relative.as_posix(), _sha256_file(path))
            for path, relative in self._iter_files(project_id)
        ]
        return _canonical_sha256(entries)

    def content_fingerprint(self, project_id: str) -> str:
        """Hash portable project contents without the local package identity."""
        entries = [
            (relative.as_posix(), _sha256_file(path))
            for path, relative in self._iter_files(project_id, include_manifest=False)
        ]
        return _canonical_sha256(entries)

    def describe(self, project_id: str) -> dict[str, object]:
        manifest = self._manifest(project_id)
        files = list(self._iter_files(project_id))
        categories: dict[str, int] = {}
        size = 0
        for path, relative in files:
            category = classify_path(relative)
            categories[category] = categories.get(category, 0) + 1
            size += path.stat().st_size
        return {
            **manifest,
            "project_id": project_id,
            "root_mode": "legacy" if project_id == self.legacy_id else "managed",
            "files": len(files),
            "bytes": size,
            "categories": categories,
            "fingerprint_sha256": self.fingerprint(project_id),
            "content_fingerprint_sha256": self.content_fingerprint(project_id),
        }

    def list_projects(self) -> list[dict[str, object]]:
        projects = [self.describe(self.legacy_id)]
        if not self.managed_root.is_dir():
            return projects
        for path in sorted(
            self.managed_root.iterdir(), key=lambda item: item.name.casefold()
        ):
            if (
                path.name.startswith(".")
                or not path.is_dir()
                or not PROJECT_ID_PATTERN.fullmatch(path.name)
            ):
                continue
            try:
                projects.append(self.describe(path.name))
            except ProjectPackageError:
                continue
        return projects

    def files(self, project_id: str) -> list[dict[str, object]]:
        return [
            {
                "path": relative.as_posix(),
                "name": relative.name,
                "category": classify_path(relative),
                "bytes": path.stat().st_size,
                "sha256": _sha256_file(path),
            }
            for path, relative in self._iter_files(project_id)
        ]

    def eda_candidates(self, project_id: str) -> list[dict[str, object]]:
        """List immutable EDA proposals without adding them to the project ZIP."""
        self.project_root(project_id)
        candidates_root = self.candidates_root
        if not candidates_root.is_dir():
            return []
        proposals: list[dict[str, object]] = []
        for manifest_path in sorted(
            candidates_root.glob("*/pcb/change.json"), reverse=True
        ):
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if (
                not isinstance(manifest, dict)
                or manifest.get("project_id") != project_id
            ):
                continue
            relative_value = manifest.get("candidate_path")
            source = manifest.get("source")
            if not isinstance(relative_value, str) or not isinstance(source, dict):
                continue
            try:
                relative = _safe_relative(relative_value)
                candidate = (candidates_root / relative).resolve()
            except (ProjectPackageError, OSError):
                continue
            if (
                not candidate.is_relative_to(candidates_root)
                or candidate.is_symlink()
                or not candidate.is_file()
            ):
                continue
            proposals.append(
                {
                    "revision_id": manifest.get("revision_id"),
                    "path": relative.as_posix(),
                    "name": candidate.name,
                    "bytes": candidate.stat().st_size,
                    "sha256": _sha256_file(candidate),
                    "source_path": source.get("path"),
                    "new_source": source.get("exists") is False,
                    "created_at": manifest.get("created_at"),
                    "validation": manifest.get("validation", {}),
                }
            )
        return proposals

    def planfile(self, project_id: str) -> dict[str, object]:
        """Return a normalized, read-only task and progress projection."""
        return load_project_plan(self.project_root(project_id), project_id)

    def resolve_file(self, project_id: str, relative: str) -> Path:
        rel = _safe_relative(relative, allow_package_manifest=True)
        root = self.project_root(project_id)
        path = root.joinpath(*rel.parts)
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise ProjectPackageError(
                "PROJECT_FILE_NOT_FOUND", f"Nie znaleziono pliku: {relative}"
            ) from exc
        if (
            not resolved.is_relative_to(root.resolve())
            or path.is_symlink()
            or not resolved.is_file()
        ):
            raise ProjectPackageError(
                "PROJECT_FILE_NOT_FOUND", f"Nie znaleziono pliku: {relative}"
            )
        return resolved

    def export_zip(self, project_id: str) -> bytes:
        """Build a deterministic, self-contained archive safe for re-import."""
        files = list(self._iter_files(project_id))
        if len(files) > MAX_ARCHIVE_FILES:
            raise ProjectPackageError(
                "PROJECT_ARCHIVE_TOO_MANY_FILES",
                "Projekt zawiera ponad 5000 plików",
            )
        expanded = sum(path.stat().st_size for path, _relative in files)
        if expanded > MAX_ARCHIVE_EXPANDED_BYTES:
            raise ProjectPackageError(
                "PROJECT_ARCHIVE_EXPANDED_TOO_LARGE",
                "Projekt zajmuje ponad 512 MiB",
            )
        wrapper = self._validate_id(project_id)
        output = io.BytesIO()
        with zipfile.ZipFile(
            output, "w", zipfile.ZIP_DEFLATED, compresslevel=9
        ) as archive:
            for path, relative in files:
                entry = zipfile.ZipInfo(
                    f"{wrapper}/{relative.as_posix()}",
                    date_time=(1980, 1, 1, 0, 0, 0),
                )
                entry.compress_type = zipfile.ZIP_DEFLATED
                entry.external_attr = (stat.S_IFREG | 0o644) << 16
                entry.create_system = 3
                archive.writestr(entry, path.read_bytes())
        content = output.getvalue()
        if len(content) > MAX_ARCHIVE_BYTES:
            raise ProjectPackageError(
                "PROJECT_ARCHIVE_TOO_LARGE",
                "Archiwum projektu przekracza limit 256 MiB",
            )
        return content

    def _new_manifest(
        self,
        project_id: str,
        name: str,
        *,
        kind: str,
        source: dict[str, object],
    ) -> dict[str, object]:
        now = _now()
        return {
            "schema_id": SCHEMA_ID,
            "schema_version": "1.0.0",
            "project_id": project_id,
            "name": name.strip(),
            "kind": kind,
            "root_mode": "managed",
            "created_at": now,
            "updated_at": now,
            "source": source,
            "license": DEFAULT_LICENSE.copy(),
            "layout": {name: name for name in STANDARD_DIRECTORIES},
            "merge_history": [],
        }

    @staticmethod
    def _write_manifest(root: Path, manifest: dict[str, object]) -> None:
        path = root / MANIFEST_RELATIVE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _stage(self) -> Path:
        self.managed_root.mkdir(parents=True, exist_ok=True)
        path = self.managed_root / f".staging-{uuid.uuid4().hex}"
        path.mkdir()
        return path

    def _initialize(self, root: Path, manifest: dict[str, object]) -> None:
        for directory in STANDARD_DIRECTORIES:
            target = root / directory
            target.mkdir(parents=True, exist_ok=True)
            (target / ".gitkeep").write_bytes(b"")
        self._write_manifest(root, manifest)
        (root / "LICENSE").write_bytes(APACHE_LICENSE_PATH.read_bytes())
        (root / "README.md").write_text(
            f"# {manifest['name']}\n\n"
            "Projekt zgodny z `wellmanifest.project-package/v1`. "
            "Źródła są odseparowane od innych projektów w tej paczce.\n\n"
            "Licencja projektu: Apache-2.0 (`LICENSE`).\n",
            encoding="utf-8",
        )
        quoted_name = json.dumps(str(manifest["name"]), ensure_ascii=False)
        (root / "planfile.yaml").write_text(
            "schema: wellmanifest.planfile/v1\n"
            f"name: {quoted_name}\n"
            f"updated_at: {_now()}\n"
            "goal: Ustalić zakres, przygotować artefakty i przejść wymagane kontrole.\n"
            "phases:\n"
            "- id: start\n"
            "  name: Start projektu\n"
            "  tasks:\n"
            "  - id: define-scope\n"
            "    title: Ustalić zakres i kryteria akceptacji\n"
            "    status: planned\n"
            "    priority: high\n"
            "  - id: add-sources\n"
            "    title: Dodać źródła projektu\n"
            "    status: planned\n"
            "    priority: high\n"
            "  - id: verify-project\n"
            "    title: Uruchomić kontrole i zapisać dowody\n"
            "    status: planned\n"
            "    priority: medium\n",
            encoding="utf-8",
        )

    def _commit_new(self, staging: Path, project_id: str) -> Path:
        target = self.managed_root / project_id
        if target.exists():
            raise ProjectPackageError(
                "PROJECT_EXISTS", f"Projekt {project_id} już istnieje"
            )
        os.replace(staging, target)
        return target

    def create(
        self, name: str, *, project_id: str | None = None, kind: str = "mixed"
    ) -> dict[str, object]:
        clean_name = name.strip()
        if not clean_name or len(clean_name) > 120:
            raise ProjectPackageError(
                "PROJECT_NAME_INVALID", "Nazwa projektu musi mieć od 1 do 120 znaków"
            )
        identifier = self._validate_id(project_id or slugify(clean_name))
        if identifier == self.legacy_id:
            raise ProjectPackageError(
                "PROJECT_EXISTS", f"Projekt {identifier} już istnieje"
            )
        staging = self._stage()
        try:
            self._initialize(
                staging,
                self._new_manifest(
                    identifier, clean_name, kind=kind, source={"type": "created"}
                ),
            )
            self._commit_new(staging, identifier)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return self.describe(identifier)

    def clone(
        self, source_id: str, name: str, *, project_id: str | None = None
    ) -> dict[str, object]:
        source = self.describe(source_id)
        clean_name = name.strip()
        if not clean_name or len(clean_name) > 120:
            raise ProjectPackageError(
                "PROJECT_NAME_INVALID", "Nazwa projektu musi mieć od 1 do 120 znaków"
            )
        identifier = self._validate_id(project_id or slugify(clean_name))
        if identifier == self.legacy_id:
            raise ProjectPackageError(
                "PROJECT_EXISTS", f"Projekt {identifier} już istnieje"
            )
        source_fingerprint = str(source["fingerprint_sha256"])
        staging = self._stage()
        try:
            for path, relative in self._iter_files(source_id, include_manifest=False):
                target = staging.joinpath(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
            (staging / "LICENSE").write_bytes(APACHE_LICENSE_PATH.read_bytes())
            manifest = self._new_manifest(
                identifier,
                clean_name,
                kind=str(source.get("kind") or "mixed"),
                source={
                    "type": "clone",
                    "project_id": source_id,
                    "fingerprint_sha256": source_fingerprint,
                },
            )
            self._write_manifest(staging, manifest)
            self._commit_new(staging, identifier)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return self.describe(identifier)

    def upload(
        self,
        project_id: str,
        relative: str,
        content: bytes,
        *,
        segregate: bool = False,
        overwrite: bool = False,
        expected_sha256: str | None = None,
    ) -> dict[str, object]:
        if len(content) > MAX_UPLOAD_BYTES:
            raise ProjectPackageError(
                "PROJECT_UPLOAD_TOO_LARGE", "Plik przekracza limit 128 MiB"
            )
        rel = _safe_relative(relative)
        if _is_transient(rel):
            raise ProjectPackageError(
                "PROJECT_PATH_RESERVED",
                "Cache, środowiska i pliki wykonywalne Pythona nie należą do paczki",
            )
        if segregate:
            rel = _segregated(rel)
        root = self.project_root(project_id)
        target = root.joinpath(*rel.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if not overwrite:
                raise ProjectPackageError(
                    "PROJECT_FILE_EXISTS", f"Plik {rel.as_posix()} już istnieje"
                )
            if target.suffix.casefold() in {".kicad_pcb", ".kicad_sch"}:
                raise ProjectPackageError(
                    "PROJECT_EDA_CANDIDATE_REQUIRED",
                    "Istniejące PCB/SCH można zmienić wyłącznie przez candidate flow",
                )
            current = _sha256_file(target)
            if not expected_sha256 or current != expected_sha256:
                raise ProjectPackageError(
                    "PROJECT_FILE_CHANGED", "Plik zmienił się od chwili wyświetlenia"
                )
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_bytes(content)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return {
            "project_id": project_id,
            "path": rel.as_posix(),
            "category": classify_path(rel),
            "bytes": len(content),
            "sha256": _sha256_bytes(content),
        }

    @staticmethod
    def _archive_entries(content: bytes) -> list[tuple[zipfile.ZipInfo, PurePosixPath]]:
        if len(content) > MAX_ARCHIVE_BYTES:
            raise ProjectPackageError(
                "PROJECT_ARCHIVE_TOO_LARGE", "Archiwum przekracza limit 256 MiB"
            )
        try:
            archive = zipfile.ZipFile(io.BytesIO(content))
        except (zipfile.BadZipFile, OSError) as exc:
            raise ProjectPackageError(
                "PROJECT_ARCHIVE_INVALID", "Plik nie jest poprawnym archiwum ZIP"
            ) from exc
        with archive:
            files = [entry for entry in archive.infolist() if not entry.is_dir()]
            if len(files) > MAX_ARCHIVE_FILES:
                raise ProjectPackageError(
                    "PROJECT_ARCHIVE_TOO_MANY_FILES",
                    "Archiwum zawiera ponad 5000 plików",
                )
            expanded = sum(entry.file_size for entry in files)
            if expanded > MAX_ARCHIVE_EXPANDED_BYTES:
                raise ProjectPackageError(
                    "PROJECT_ARCHIVE_EXPANDED_TOO_LARGE",
                    "Archiwum rozpakowuje się do ponad 512 MiB",
                )
            result: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
            for entry in files:
                mode = entry.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise ProjectPackageError(
                        "PROJECT_ARCHIVE_SYMLINK",
                        f"Archiwum zawiera dowiązanie: {entry.filename}",
                    )
                if entry.file_size and (
                    not entry.compress_size
                    or entry.file_size / entry.compress_size > MAX_COMPRESSION_RATIO
                ):
                    raise ProjectPackageError(
                        "PROJECT_ARCHIVE_RATIO",
                        f"Podejrzany stopień kompresji: {entry.filename}",
                    )
                relative = _safe_relative(entry.filename, allow_package_manifest=True)
                if relative.parts[0] == "__MACOSX" or relative.name == ".DS_Store":
                    continue
                if relative.parts[0] in TRANSIENT_TOP_LEVEL:
                    continue
                if _is_transient(relative):
                    continue
                result.append((entry, relative))
            return result

    def import_zip(
        self,
        name: str,
        content: bytes,
        *,
        project_id: str | None = None,
        segregate: bool = False,
    ) -> dict[str, object]:
        clean_name = name.strip()
        if not clean_name or len(clean_name) > 120:
            raise ProjectPackageError(
                "PROJECT_NAME_INVALID", "Nazwa projektu musi mieć od 1 do 120 znaków"
            )
        identifier = self._validate_id(project_id or slugify(clean_name))
        entries = self._archive_entries(content)
        common_root = None
        if entries and all(len(relative.parts) > 1 for _, relative in entries):
            roots = {relative.parts[0] for _, relative in entries}
            common_root = next(iter(roots)) if len(roots) == 1 else None
        staging = self._stage()
        targets: dict[PurePosixPath, str] = {}
        try:
            manifest = self._new_manifest(
                identifier,
                clean_name,
                kind="mixed",
                source={
                    "type": "zip",
                    "archive_sha256": _sha256_bytes(content),
                    "segregated": segregate,
                },
            )
            self._initialize(staging, manifest)
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                for entry, relative in entries:
                    if common_root:
                        relative = PurePosixPath(*relative.parts[1:])
                    if relative == PurePosixPath(MANIFEST_RELATIVE.as_posix()):
                        continue
                    if segregate:
                        relative = _segregated(relative)
                    data = archive.read(entry)
                    digest = _sha256_bytes(data)
                    previous = targets.get(relative)
                    if previous is not None and previous != digest:
                        raise ProjectPackageError(
                            "PROJECT_ARCHIVE_CONFLICT",
                            f"Dwa pliki trafiają do {relative.as_posix()}",
                        )
                    targets[relative] = digest
                    target = staging.joinpath(*relative.parts)
                    replaceable_skeleton = {
                        PurePosixPath("README.md"),
                        PurePosixPath("planfile.yaml"),
                    }
                    if (
                        target.exists()
                        and target.name != ".gitkeep"
                        and relative not in replaceable_skeleton
                    ):
                        if target.read_bytes() == data:
                            continue
                        raise ProjectPackageError(
                            "PROJECT_ARCHIVE_CONFLICT",
                            "Plik "
                            f"{relative.as_posix()} koliduje ze szkieletem projektu",
                        )
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
            self._commit_new(staging, identifier)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return self.describe(identifier)

    @staticmethod
    def _conflict_name(
        relative: PurePosixPath, source_id: str, occupied: set[str]
    ) -> PurePosixPath:
        suffix = "".join(relative.suffixes)
        stem = relative.name[: -len(suffix)] if suffix else relative.name
        parent = relative.parent
        counter = 1
        while True:
            marker = (
                f"--from-{source_id}"
                if counter == 1
                else f"--from-{source_id}-{counter}"
            )
            candidate = parent / f"{stem}{marker}{suffix}"
            if candidate.as_posix() not in occupied:
                return candidate
            counter += 1

    def merge_plan(
        self,
        target_id: str,
        source_id: str,
        *,
        conflict_strategy: Literal["reject", "keep_both"] = "reject",
    ) -> dict[str, object]:
        if target_id == source_id:
            raise ProjectPackageError(
                "PROJECT_MERGE_SELF", "Projekt nie może zostać scalony sam ze sobą"
            )
        source_files = {
            rel.as_posix(): (path, _sha256_file(path))
            for path, rel in self._iter_files(source_id, include_manifest=False)
        }
        target_files = {
            rel.as_posix(): (path, _sha256_file(path))
            for path, rel in self._iter_files(target_id, include_manifest=False)
        }
        occupied = set(target_files)
        actions: list[dict[str, object]] = []
        for relative, (path, digest) in source_files.items():
            existing = target_files.get(relative)
            if existing is None:
                action = {
                    "action": "add",
                    "source": relative,
                    "target": relative,
                    "sha256": digest,
                    "bytes": path.stat().st_size,
                }
                occupied.add(relative)
            elif existing[1] == digest:
                action = {
                    "action": "identical",
                    "source": relative,
                    "target": relative,
                    "sha256": digest,
                    "bytes": path.stat().st_size,
                }
            else:
                target = relative
                if conflict_strategy == "keep_both":
                    target = self._conflict_name(
                        PurePosixPath(relative), source_id, occupied
                    ).as_posix()
                    occupied.add(target)
                action = {
                    "action": "conflict",
                    "source": relative,
                    "target": target,
                    "source_sha256": digest,
                    "target_sha256": existing[1],
                    "bytes": path.stat().st_size,
                }
            actions.append(action)
        core = {
            "schema_id": "artifact-viewer.project-merge-plan/v1",
            "target_project_id": target_id,
            "source_project_id": source_id,
            "target_fingerprint_sha256": self.fingerprint(target_id),
            "source_fingerprint_sha256": self.fingerprint(source_id),
            "conflict_strategy": conflict_strategy,
            "actions": actions,
        }
        return {
            **core,
            "summary": {
                "add": sum(item["action"] == "add" for item in actions),
                "identical": sum(item["action"] == "identical" for item in actions),
                "conflict": sum(item["action"] == "conflict" for item in actions),
            },
            "plan_sha256": _canonical_sha256(core),
        }

    def merge(
        self,
        target_id: str,
        source_id: str,
        *,
        conflict_strategy: Literal["reject", "keep_both"] = "reject",
        expected_plan_sha256: str,
    ) -> dict[str, object]:
        if target_id == self.legacy_id:
            raise ProjectPackageError(
                "PROJECT_LEGACY_MERGE_FORBIDDEN",
                "Projekt bazowy jest chroniony; sklonuj „klawiatura” i scalaj do kopii",
            )
        plan = self.merge_plan(
            target_id, source_id, conflict_strategy=conflict_strategy
        )
        if plan["plan_sha256"] != expected_plan_sha256:
            raise ProjectPackageError(
                "PROJECT_MERGE_STALE",
                "Plan scalania nie odpowiada bieżącej zawartości projektów",
            )
        if conflict_strategy == "reject" and plan["summary"]["conflict"]:
            raise ProjectPackageError(
                "PROJECT_MERGE_CONFLICT",
                "Plan zawiera konflikty; wybierz keep_both albo rozwiąż je ręcznie",
            )
        target = self.project_root(target_id)
        source = self.project_root(source_id)
        staging = self._stage()
        backup = self.managed_root / f".backup-{target_id}-{uuid.uuid4().hex}"
        try:
            shutil.rmtree(staging)
            shutil.copytree(target, staging, symlinks=False)
            for action in plan["actions"]:
                if action["action"] == "identical":
                    continue
                source_path = source.joinpath(
                    *PurePosixPath(str(action["source"])).parts
                )
                target_path = staging.joinpath(
                    *PurePosixPath(str(action["target"])).parts
                )
                target_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_path, target_path)
            manifest = self._manifest(target_id)
            history = list(manifest.get("merge_history") or [])
            history.append(
                {
                    "at": _now(),
                    "source_project_id": source_id,
                    "source_fingerprint_sha256": plan["source_fingerprint_sha256"],
                    "plan_sha256": plan["plan_sha256"],
                    "conflict_strategy": conflict_strategy,
                }
            )
            manifest["merge_history"] = history[-100:]
            manifest["updated_at"] = _now()
            self._write_manifest(staging, manifest)
            os.replace(target, backup)
            try:
                os.replace(staging, target)
            except Exception:
                os.replace(backup, target)
                raise
            shutil.rmtree(backup)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            if backup.exists() and not target.exists():
                os.replace(backup, target)
            raise
        return {"status": "merged", "plan": plan, "project": self.describe(target_id)}
