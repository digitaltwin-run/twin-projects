"""Portable, isolated project workspaces for digital-twin applications."""

from .packages import (
    APACHE_LICENSE_PATH,
    MANAGED_RELATIVE,
    MANIFEST_RELATIVE,
    PROJECT_PACKAGE_SCHEMA_PATH,
    SCHEMA_ID,
    ProjectPackageError,
    ProjectPackageStore,
    classify_path,
    slugify,
)
from .planfile import load_project_plan

__all__ = [
    "APACHE_LICENSE_PATH",
    "MANAGED_RELATIVE",
    "MANIFEST_RELATIVE",
    "PROJECT_PACKAGE_SCHEMA_PATH",
    "SCHEMA_ID",
    "ProjectPackageError",
    "ProjectPackageStore",
    "classify_path",
    "load_project_plan",
    "slugify",
]

__version__ = "0.1.0"
