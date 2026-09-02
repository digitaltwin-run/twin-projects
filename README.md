# twin-projects

`twin-projects` is the Apache-2.0 project-workspace component used by
TwinStudio and its clients. It owns the portable project-package format,
isolated project folders, deterministic ZIP import/export, safe uploads and
merges, content fingerprints, and read-only Planfile normalization.

The component deliberately does not render a web interface and does not edit
KiCad sources. TwinStudio exposes it through an authenticated API; clients such
as Viewer are responsible only for presentation and user interaction.

## Install

```bash
python -m pip install twin-projects
```

For a repository checkout:

```bash
python -m pip install -e '.[dev]'
pytest -q
```

## Storage model

Given an artifacts root, the adopted legacy workspace remains at that root.
Managed workspaces live under `.projects/<project-id>`. Every managed project
has `.wellmanifest/project-package.json`, a fixed Apache-2.0 license, standard
domain folders, and an initial `planfile.yaml`.

Runtime journals, caches, candidate revisions, and other machine-local state
are excluded from portable exports. KiCad writes remain the responsibility of
TwinStudio's candidate/review/promotion workflow.

`ProjectPackageStore(..., candidates_root=...)` lets the owning service keep
candidate revisions in a separate runtime volume while still exposing them in
workspace details. If omitted, it retains the compatible
`<artifacts-root>/eda-candidates` default.

## Dependency direction

```text
Viewer (UI client) -> TwinStudio API -> twin-projects
                                   -> twin-kicad
                                   -> twinapi
```

`twin-projects` has no dependency on Viewer or TwinStudio.
