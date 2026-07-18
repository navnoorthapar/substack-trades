# Launch and operations runbook

Target launch: 2026-07-19
Production: <https://navnoorthapar.github.io/substack-trades/>

## 1. Release authority and prerequisites

- The release owner has authenticated Git and GitHub CLI access to the correct
  `navnoorthapar/substack-trades` repository.
- GitHub Pages is configured to deploy through GitHub Actions and HTTPS is
  enforced in repository settings.
- `main` remains protected against force pushes, deletion, and non-linear
  history; Actions has permission to deploy Pages.
- The scheduled Mac is logged in, connected to the internet, and allowed to run
  `com.navnoor.substacktrades` in the background.
- No secrets, private datasets, or confidential decision packets are committed.

## 2. Preflight gate

Run from a clean `main` worktree:

```bash
git status --short
python3 -m unittest discover -s . -p 'test_*.py' -v
python3 validate_pipeline.py \
  --articles articles_index.json \
  --trades trades_extracted.json \
  --manifest snapshot_manifest.json
ruff check *.py
mypy --cache-dir "${TMPDIR:-/tmp}/nrt-mypy-cache"
PREVIEW_DIR="$(mktemp -d)/site"
SITE_OUTPUT_DIR="$PREVIEW_DIR" SITE_REVISION="$(git rev-parse HEAD)" python3 build_site.py
python3 validate_inline_scripts.py "$PREVIEW_DIR/index.html"
./automation_status.sh
gh auth status
```

Confirm the test suite, tracked data manifest, lint, type check, inline
JavaScript compilation, updater, snapshot freshness, and GitHub authentication
all pass. On the scheduled Mac, repeat data validation with
`--posts all_sources_posts.json` to bind the release to the ignored full-source
cache. Confirm the generated artifact contains exactly `index.html`, both
deferred JSON assets, `robots.txt`, `sitemap.xml`, `site.webmanifest`,
`favicon.svg`, and `og.jpg`.

## 3. Release

1. Push the reviewed commit to `main`.
2. Watch **Validate and Deploy Pages**. The quality job must pass before the
   immutable Pages artifact can deploy.
3. Require the post-deploy smoke step to confirm HTTPS, exact Git revision,
   snapshot checksum/counts, exact HTML, both deferred JSON files, and the
   combined discovery/social support bundle.
4. Record the successful commit and create an annotated launch tag only after
   production smoke succeeds.
5. Open production and manually verify the default workbench, a recent article,
   an older deferred dossier, observation loading, keyboard-only navigation,
   mobile layout, light/dark themes, print preview, and an intentionally offline
   asset failure. Do not enter confidential data during testing.

If a push queues no run, use:

```bash
gh workflow run update.yml --ref main
gh run list --workflow update.yml --limit 5
```

## 4. Discovery setup

- Add the production property to Google Search Console using an owner-controlled
  verification method.
- Submit
  `https://navnoorthapar.github.io/substack-trades/sitemap.xml` and verify it is
  fetched successfully.
- Confirm `robots.txt`, the canonical URL, social preview image, manifest, and
  favicon resolve over HTTPS.
- Set the GitHub repository description, homepage, and topics if they are still
  blank. This requires an authenticated repository owner.
- Use GitHub's aggregate repository traffic and Search Console for discovery
  health. Do not add reader-level analytics without revisiting this privacy
  policy and obtaining a clear product need.

## 5. Routine monitoring

- Local ingestion runs at 09:00, 13:00, and 22:00 Asia/Kolkata and after login.
- The production watchdog runs every four hours and rejects snapshots older
  than 16 hours.
- At least daily during launch week, inspect the latest updater log, deployment,
  watchdog, source counts, per-source health, and published release timestamp.
- Review Dependabot updates and production failures promptly. Do not merge a pin
  update without the full gate.

## 6. Incident response

Severity guidance:

- **Critical:** misleading/corrupt research data, sensitive-data exposure,
  malicious content execution, or production serving an unverified artifact.
- **High:** terminal unavailable, stale beyond policy, major navigation failure,
  or inability to inspect original evidence.
- **Normal:** isolated presentation defects with a safe workaround.

For Critical or High incidents:

1. Preserve the failing workflow URL, commit, UTC time, browser/OS, console text,
   and source-health state. Do not include confidential reader data.
2. Stop further automatic publication if continued runs could worsen impact.
3. Revert the offending commit with `git revert`; never rewrite shared history.
4. Push the revert and require the complete quality and exact-production smoke
   gates. Deployment accepts only the current `main` ref, so restore any older
   tree or snapshot through a new reviewed commit on `main`; do not dispatch a
   tag or detached SHA as a rollback.
5. Verify production and the watchdog, then communicate scope and resolution.
6. Document root cause, detection gap, corrective test, and follow-up owner.

## 7. Launch completion record

Record these values in the launch notes:

- release commit and annotated tag;
- deployment and post-deploy smoke run URLs;
- snapshot `checked_at`, article count, and observation count;
- successful desktop/mobile, keyboard, theme, print, offline, and source-link
  spot checks;
- Search Console sitemap status;
- any accepted limitation, owner, and target date.

Known platform boundaries at launch: GitHub Pages controls response headers;
inline styles require CSP `unsafe-inline`; workflow packets are plaintext but
limited to a top-level tab session, while exports remain plaintext files; and
the project deliberately has no reader-level analytics.
