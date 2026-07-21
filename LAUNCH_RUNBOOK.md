# Launch and operations runbook

Launch certification: 2026-07-21 (original target: 2026-07-19)
Production: <https://navnoorthapar.github.io/substack-trades/>

## 1. Release authority and prerequisites

- The release owner has authenticated Git and GitHub CLI access to the correct
  `navnoorthapar/substack-trades` repository.
- GitHub Pages is configured to deploy through GitHub Actions and HTTPS is
  enforced in repository settings.
- `main` remains protected against force pushes, deletion, and non-linear
  history; Actions has permission to deploy Pages.
- An active repository tag ruleset matches `refs/tags/launch-*` and blocks tag
  updates and deletion without bypass actors; it deliberately allows creation.
- The scheduled Mac is logged in, connected to the internet, and allowed to run
  `com.navnoor.substacktrades` in the background.
- No secrets, private datasets, or confidential decision packets are committed.

## 2. Preflight gate

Run from a clean `main` worktree:

```bash
test -z "$(git status --porcelain)"
python3 -m unittest discover -s . -p 'test_*.py' -v
python3 validate_pipeline.py \
  --articles articles_index.json \
  --trades trades_extracted.json \
  --manifest snapshot_manifest.json
ruff check *.py
mypy --cache-dir "${TMPDIR:-/tmp}/nrt-mypy-cache"
python3 -m py_compile *.py
for file in *.sh; do bash -n "$file"; done
plutil -lint launchd/com.navnoor.substacktrades.plist
PREVIEW_DIR="$(mktemp -d)"
SITE_OUTPUT_DIR="$PREVIEW_DIR" SITE_REVISION="$(git rev-parse HEAD)" python3 build_site.py
python3 validate_inline_scripts.py "$PREVIEW_DIR/index.html"
test "$(find "$PREVIEW_DIR" -maxdepth 1 -type f | wc -l | tr -d ' ')" -eq 8
rm -r "$PREVIEW_DIR"
git diff --check
./automation_status.sh
gh auth status
```

Confirm the test suite, tracked data manifest, lint, type check, Python, shell,
and plist syntax, inline JavaScript compilation, updater, snapshot freshness,
clean diff, and GitHub authentication all pass. On the scheduled Mac, repeat
data validation with
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
4. Dispatch **Monitor Published Research** against the same `main` commit with
   `gh workflow run watchdog.yml --ref main`; require its exact-release and
   freshness checks to pass.
5. Open production and manually verify the default workbench, a recent article,
   an older deferred dossier, observation loading, mobile layout, light/dark
   themes, print preview, and source links. Execute the keyboard/focus and
   offline/deferred-asset recovery tests against the generated client. Do not
   enter confidential data during testing.
6. Run Lighthouse against Latest, Evidence, Library, and Queue on the exact
   production release. Record performance plus Accessibility, Best Practices,
   and SEO results; inspect 375 px, 768 px, and desktop layouts for overflow and
   touch-target regressions.
7. Create the annotated launch tag only after production smoke, the independent
   watchdog, and the production checks above succeed. Put the exact release SHA
   and both workflow URLs in the tag annotation.

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
- Confirm the project-path `substack-trades/robots.txt`, canonical URL, social
  preview image, manifest, and favicon resolve over HTTPS. GitHub project Pages
  cannot publish an origin-root `navnoorthapar.github.io/robots.txt`; that
  accepted boundary is tracked in `ISSUES.md`.
- Set the GitHub repository description, homepage, and topics if they are still
  blank. This requires an authenticated repository owner.
- Use GitHub's aggregate repository traffic and Search Console for discovery
  health. Do not add reader-level analytics without revisiting this privacy
  policy and obtaining a clear product need.

## 5. Routine monitoring

- Local ingestion runs at 09:00, 13:00, and 22:00 Asia/Kolkata and after login.
- The production watchdog is scheduled every four hours and rejects snapshots
  older than 16 hours.
- At least daily during launch week, inspect the latest updater log, deployment,
  watchdog, source counts, per-source health, and published release timestamp.
- Monthly, confirm `watchdog.yml` remains enabled with
  `gh workflow view watchdog.yml`. GitHub can disable public-repository
  schedules after 60 days without repository activity; if needed, restore it
  with `gh workflow enable watchdog.yml` and immediately dispatch a manual run.
  See [GitHub's scheduled-workflow policy](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).
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

Certification date: **2026-07-21**

- **Protected release identity:** annotated tag `launch-2026-07-21`. Its
  annotation is the authoritative durable record of the final release SHA, the
  exact **Validate and Deploy Pages** URL, the independent **Monitor Published
  Research** URL, and the final Lighthouse scores. The tag must not exist unless
  every Section 3 gate has passed for its peeled commit. Active ruleset
  `19448243` prevents matching launch tags from being updated or deleted and has
  no bypass actors.
- **Implementation and data lineage:** the release contains the updater/print
  correction from `93b18f2d45d7888f89bdee09e7ae57f050552203` and the fresh
  publication snapshot from `551ce56c345f97e95239eb132e7a90a515af06ce`.
  Their exact-production deployments passed in GitHub Actions runs
  [29852762167](https://github.com/navnoorthapar/substack-trades/actions/runs/29852762167)
  and
  [29853041159](https://github.com/navnoorthapar/substack-trades/actions/runs/29853041159).
- **Snapshot:** checked `2026-07-21T17:26:54Z`; 363 unique articles and 1,268
  source observations. Substack was healthy in `complete_api` mode with 229
  included articles. Medium was healthy in `complete_archive` mode with 361
  authored posts fetched and 134 unique articles included after cross-post
  deduplication. The latest publication remained 2026-07-11; the successful
  source checks prove this is current source state, not a stalled refresh.
- **Automated gates:** all 177 tests passed. Tracked and strict cached-source
  validation, Ruff, mypy, Python compilation, shell syntax, plist validation,
  deterministic eight-file build, inline JavaScript compilation, exact release
  fingerprints, and post-deploy asset/support-bundle smoke checks passed.
- **Production quality baseline:** on exact deployed product/data revision
  `551ce56c345f97e95239eb132e7a90a515af06ce`, Lighthouse performance scored 98
  Latest, 93 Evidence, 93 Library, and 92 Queue. Accessibility, Best Practices,
  and SEO scored 100 on all four routes, with zero failing accessibility audits.
  The final documentation-only certification revision is rerun before tagging;
  its exact scores are retained in the protected tag annotation and must keep
  performance at or above 90 and the other three categories at 100.
- **Interaction and layout:** Latest, Evidence, Library, and Queue were checked
  at 375 px, 768 px, and 1,440 px with no horizontal overflow; mobile and tablet
  layouts expose no visible non-inline control below 24 CSS px, while mobile
  controls use 44 px hit targets. Financial Times-inspired light and Bloomberg
  Terminal-inspired dark themes were visually checked. Automated keyboard/focus
  semantics passed; empty search and queue recovery actions were verified;
  network, storage, malformed-import, timeout, and stale-shell paths fail closed
  with bounded recovery.
- **Print and provenance:** an exact-production six-page brief was rendered to
  PDF after the print fix. The rendered and inspected pages show no repeated
  skip-link overlay or visible clipping; text extraction confirms the hidden
  link is absent, and source identity/citation remains present.
- **Operations:** the scheduled LaunchAgent completed a real refresh with latest
  exit 0; `automation_status.sh` passed. Refreshes run at 09:00, 13:00, and 22:00
  Asia/Kolkata and after login; the independent watchdog is scheduled every
  four hours.
- **Accepted follow-up:** Search Console ownership and sitemap submission require
  an owner-authenticated browser action and are not claimed complete. Owner:
  repository owner. Target: **2026-07-28**. The deployed canonical/meta robots,
  project sitemap, project-path robots file, manifest, favicon, and social image
  already validate. No reader-level analytics is shipped; GitHub aggregate
  traffic and Search Console remain the privacy-approved measurement approach.

Known platform boundaries at launch: GitHub Pages controls response headers and
the origin-root robots file; inline styles require CSP `unsafe-inline`;
workflow packets are plaintext but limited to a top-level tab session, while
exports remain plaintext files; hash-selected views share canonical social
metadata; and the project deliberately has no reader-level analytics.
