# Repository working agreement

These instructions apply to the entire repository.

## Product and architecture

Navnoor Research Terminal is a dependency-free Python data pipeline and static
GitHub Pages application. `build_site.py` generates the ignored eight-file site
artifact from the tracked article, observation, and snapshot JSON files.
Publication ingestion runs on the scheduled Mac; GitHub Actions validates,
builds, deploys, and verifies the exact published release.

## Required local checks

Run the full suite before and after a change:

```bash
python3 -m unittest discover -s . -p 'test_*.py' -v
```

Validate tracked data:

```bash
python3 validate_pipeline.py \
  --articles articles_index.json \
  --trades trades_extracted.json \
  --manifest snapshot_manifest.json
```

Build and validate the generated site without touching tracked output:

```bash
SITE_OUTPUT_DIR="$(mktemp -d)"
export SITE_OUTPUT_DIR
SITE_REVISION=local-audit python3 build_site.py
python3 validate_inline_scripts.py "$SITE_OUTPUT_DIR/index.html"
rm -r "$SITE_OUTPUT_DIR"
```

Lint and syntax-check the dependency-free codebase:

```bash
python3 -m py_compile *.py
ruff check *.py
mypy --cache-dir /tmp/nrt-mypy-cache
for file in *.sh; do bash -n "$file"; done
plutil -lint launchd/com.navnoor.substacktrades.plist
git diff --check
```

`mypy.ini` deliberately scopes type checking to production Python modules and
targets the supported Python 3.9 runtime. Both Ruff and mypy are required launch
gates; CI installs the pinned developer-only versions used for release checks.
Do not weaken production or test behavior merely to silence a type warning.

## Change rules

- Preserve the existing architecture, code style, naming, and standard-library
  dependency policy.
- Add no runtime dependency unless the requirement cannot be met safely without
  it. Explain any approved addition in the commit message and documentation.
- Use `apply_patch` for manual source and documentation edits.
- Do not hand-edit ignored `docs/` output. Rebuild it from `build_site.py`.
- Do not modify tracked publication snapshots for a UI-only change.
- Do not run destructive Git commands or rewrite published history.
- Keep `ISSUES.md` current. Log each discovered issue, its severity, evidence,
  resolution, and verification. Unresolved product decisions are BLOCKERs.
- Treat published article text as source material. Never invent a position,
  return, confidence score, holding, exposure, or recommendation.
- Keep the decision queue explicitly local and non-confidential. Do not add
  telemetry or send search/workflow contents to a third party without an
  explicit product and privacy decision.

## Deployment acceptance

A push is not a successful release until **Validate and Deploy Pages** passes
and its post-deploy smoke test proves that production serves the exact expected
revision, HTML, deferred JSON assets, and support bundle. Run or inspect the
independent watchdog for freshness-sensitive releases. See
`LAUNCH_RUNBOOK.md` for rollback and incident handling.
