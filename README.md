# Navnoor Research Terminal

This project collects authored research from `navnoorbawa.substack.com` and
`medium.com/@navnoorbawa`, adds privacy-safe public catalogue metadata from
Patreon and FX Empire, extracts structured investment observations, and
publishes the institutional research terminal at
<https://navnoorthapar.github.io/substack-trades/>.

## Architecture

Substack rejects cloud-datacenter traffic, so publication ingestion stays on a
logged-in Mac with a residential connection. Website builds and production
deployment run independently on GitHub Actions:

```text
Scheduled Mac
  Substack API --------> all_posts.json --------\
  Medium archive -----> medium_posts.json -------+-> conservative dedupe/cross-linking
  Patreon public index -> patreon_registry.json -+                 |
  Reviewed FX byline --> fxempire_registry.json -/                 +-> four-source articles_index.json
                                                                  +-> trades_extracted.json
                                                                  +-> snapshot_manifest.json
                                                                  +-> strict validation + tests
                                                                  +-> commit tracked data
                                                                            |
                                                                            v
GitHub Actions
  tracked snapshot -> validation + tests -> terminal shell + deferred research JSON
                                         -> six versioned data/*.json endpoints
                                         -> article-specific /cards/*.png + /a/*.html
                                         -> robots/sitemap/manifest/favicon/social image
                                         -> one immutable Pages artifact
                                         -> atomic deployment + exact smoke test
```

Medium's public author archive is paginated; its ten-item RSS feed can extend a
previous complete catalogue when the archive is temporarily unavailable. A
simultaneous archive and RSS outage fails closed and preserves the last good
snapshot. Cross-posts are matched using Medium's explicit Substack notice,
normalized titles, subtitles plus dates, conservative similarity, and reviewed
mappings in `medium_dedupe_overrides.json`. Substack remains the canonical card
for cross-posts; only Medium-only articles are added.

`all_posts.json` and `all_sources_posts.json` stay local. The tracked pipeline
state includes `medium_posts.json`, `patreon_registry.json`,
`fxempire_registry.json`, `registry_crosslink_overrides.json`,
`articles_index.json`, `trades_extracted.json`, `snapshot_manifest.json`, and
`.direction_cache.json`; retaining the catalogues prevents a temporary source
failure from erasing older articles. Patreon and FX Empire records are
metadata-only: the project does not scrape or republish their article bodies.
Production builds consume the validated article and observation snapshots. The
manifest binds exact input bytes, counts, publication freshness, and per-channel
fetch health. Deferred assets carry the same checksum and are rejected by the
browser if their release identity, record IDs, article ownership, or required
fields do not match. Generated site files are intentionally ignored:
every production artifact is rebuilt, tested, and deployed without a bot commit
or a second source of truth.

The core pipeline needs Python 3.9+, Node.js for generated-script compilation,
Git with authenticated write access to `origin`, and network access to
Substack, Medium, Patreon, and GitHub. FX Empire is a manually reviewed byline
registry rather than an automated scraper. The project has no third-party
Python runtime dependencies.
Ollama with `qwen2.5:14b` is optional; without it, refreshes preserve cached
classifications and keep the regex-only direction for new residuals.

## Machine-readable data layer

Each deployment publishes six static, UTF-8 JSON endpoints from the same
validated four-source snapshot as the terminal:

- [`data/articles_index.json`](https://navnoorthapar.github.io/substack-trades/data/articles_index.json) — the complete Substack, Medium, Patreon, and FX Empire catalogue with bounded briefs.
- [`data/latest.json`](https://navnoorthapar.github.io/substack-trades/data/latest.json) — the deterministic newest-20 projection.
- [`data/manifest.json`](https://navnoorthapar.github.io/substack-trades/data/manifest.json) — schema version, dataset identity, freshness, counts, and endpoint discovery.
- [`data/search_index.json`](https://navnoorthapar.github.io/substack-trades/data/search_index.json) — a compact deterministic entity/topic index.
- [`data/related.json`](https://navnoorthapar.github.io/substack-trades/data/related.json) — five explainable related-research candidates per article.
- [`data/families.json`](https://navnoorthapar.github.io/substack-trades/data/families.json) — the deterministic seven-family catalogue partition.

There is no API server or write method. Fetch the manifest first, reject schema
versions your integration does not support, and treat `dataset_version` as the
identity of the complete snapshot. Manifest endpoint entries are
project-relative; resolve them against
`https://navnoorthapar.github.io/substack-trades/`, not the GitHub Pages origin
alone. Search-index integers are positions in that single `search_index.json`
release; immediately resolve them to `source:slug` and never retain them across
dataset versions. For example:

```bash
BASE='https://navnoorthapar.github.io/substack-trades'
curl --fail --silent --show-error "$BASE/data/manifest.json" | python3 -m json.tool
curl --fail --silent --show-error "$BASE/data/latest.json" | python3 -m json.tool
curl --fail --silent --show-error "$BASE/data/articles_index.json" > /tmp/navnoor-research.json
```

Downstream research tools can poll `latest.json`, join full records by
`source:slug`, use `search_index.json` for retrieval, review the reasons in
`related.json` before creating links, and use `families.json` for coverage
planning. These signals organize published research; they are not positions,
recommendations, confidence scores, or performance claims. Every catalogue row
also produces `/cards/<slug>.png` and `/a/<slug>.html` for an article-specific
social preview and crawler-readable entry point. Content-bearing stubs enter the
matching terminal dossier; registry-only stubs open the original public source.
The exact field, ranking, versioning, registry, privacy, and share-asset
contracts are in [SCHEMA.md](SCHEMA.md).

## Product scope: institutional research intake

This is an institutional research-intake and human-diligence terminal. It helps
an owner, CIO, portfolio manager, trader, or quantitative researcher discover
published ideas, inspect the exact supporting passage, triage uncertainty, and
turn a candidate into a reviewable decision packet. It is deliberately not a
portfolio-management, order-management, risk, accounting, compliance, or
investor-reporting system.

That boundary and diligence structure are informed by
[CFA Institute's manager-selection framework](https://www.cfainstitute.org/insights/professional-learning/refresher-readings/2026/investment-manager-selection),
the [AIMA 2025 manager due-diligence questionnaire](https://www.aima.org/article/presenting-the-2025-edition.html),
[CFA Standard V(A)](https://www.cfainstitute.org/standards/professionals/code-ethics-standards/standards-of-practice-v-a),
[CFA Standard V(C)](https://www.cfainstitute.org/standards/professionals/code-ethics-standards/standards-of-practice-v-c),
and the [SEC investment-adviser marketing guide](https://www.sec.gov/resources-small-businesses/small-business-compliance-guides/investment-adviser-marketing).
Those references shape questions, evidence retention, and disclosure boundaries;
they do not certify the product or establish legal compliance.
The terminal supports published-source discovery and pre-decision research. It
does not manufacture NAV/P&L, attribution, exposure, leverage, VaR, stress,
liquidity, funding, counterparty, capacity, execution, compliance, or investor
metrics without the connected books and records required to calculate them.

The default Institutional Article Workbench is built around the article data:
the first eligible authored passage, contextual evidence, mechanism,
limitations, falsifiers, implementation, cited checkpoints, and exact source
provenance. Its evidence ledger keeps detected numeric tokens attached to their
original passage; tokens are lexical, deduplicated, capped, and never presented
as normalized or comparable facts. Duplicate spans are collapsed by source
identity, related research requires an exact mentioned-entity or underlying
overlap, and excerpt gaps are marked not assessable rather than absent.
Older article dossiers are release-bound deferred assets. The browser rejects
missing, unknown, malformed, or hash-mismatched dossier records before installing
any of them, and it never converts an unavailable dossier into a claim that
evidence is absent. Exact SHA-256 digests for both deferred assets are embedded
in the tested HTML, so swapped, reordered, truncated, or otherwise altered asset
bytes fail closed before JSON installation. Deployment additionally requires a
three-way match between each independently recorded deferred-asset build digest,
its HTML metadata binding, and the bytes fetched from production. The complete
HTML document is also matched byte-for-byte to its tested build digest, and every
generated inline script must pass Node.js syntax validation before upload. The
larger observation archive loads only when a selected view or filter needs it,
avoiding a late rerender of the active brief.

The briefing navigation remains complete when the desktop rail collapses, and
the print/PDF layout preserves the authored IC decision sheet and public
checkpoints while removing tab-session workflow fields.

The default Light theme uses a Financial Times-inspired editorial grammar:
warm paper surfaces, dark ink, serif research headlines, claret hierarchy, and
restrained teal interaction cues. Dark mode uses a Bloomberg Terminal-inspired
grammar: near-black panes, square geometry, compact sans/monospace controls,
amber command states, and cyan information cues. These are independent visual
references only; the project does not copy either product or imply affiliation.
Both modes use the same semantic data colors, text labels, focus treatment, and
contrast gates.

The Evidence Monitor and Research Library provide fast passage-level review.
Directional labels describe parsed language, not an actor, verified position,
exposure, conviction, or current view. Decision Workflow stores an 18-part
analyst packet: eight investment-case fields, six self-attested control gates,
and four workflow controls. Coverage means only that fields were populated; it
is not approval, conviction, investability, or proof that a control was
completed.

Each workflow packet retains a bounded source snapshot and dataset checksum.
If a later extraction changes or removes the observation ID, the packet remains
visible as an orphaned source snapshot instead of silently disappearing. Backup
imports merge with existing work, and removal archives a packet rather than
destroying its history. "New since last review" advances only when the user
explicitly marks the review baseline; simply opening or reloading the site does
not acknowledge new research.

All passage-scored evidence fields are derived from the exact passage shown in
the inspector—never from hidden adjacent paragraphs. Mentioned-entity labels may
also come from the displayed article title and retain the original extracted
mention. Truncated captures carry an explicit flag. Direction classification
abstains when a passage negates or rejects a trade signal unless it subsequently
states an explicit affirmative position. These controls reduce false precision;
they do not replace reading the original article or obtaining independent
evidence.

Workflow packets use plaintext `sessionStorage`, which is isolated to the
current top-level browser tab and survives reloads only until that tab session
closes. Explicit exports are plaintext backups. On first use, the terminal
states these boundaries and prohibits confidential or regulated entries. A
valid legacy origin-wide queue is transactionally moved into the tab session
and removed from persistent storage; malformed legacy records fail closed and
can be preserved before cleanup. Restore keeps a tab-scoped rollback across
reloads. These packets are not an authenticated, shared, encrypted, or immutable
enterprise audit record. See [PRIVACY.md](PRIVACY.md) and
[SECURITY.md](SECURITY.md).

## Install the scheduled updater

```bash
./install_automation.sh
```

The installer copies the versioned LaunchAgent into
`~/Library/LaunchAgents`, loads it, verifies it, and starts one refresh. It then
runs at **9:00 AM, 1:00 PM, and 10:00 PM local time** (09:00, 13:00, and
22:00) and once after login.

macOS may block a new background process. Open **System Settings -> General ->
Login Items & Extensions -> Allow in Background**, enable the `bash`/Unknown
Developer item associated with `com.navnoor.substacktrades`, then rerun the
installer. The Mac must be logged in for this user LaunchAgent to run.

## Operate the pipeline

Run an immediate refresh:

```bash
./refresh.sh
```

Bypass only the 30-minute duplicate-run guard:

```bash
FORCE_REFRESH=1 ./refresh.sh
```

Check the local updater, freshness marker, Pages mode, and latest deployment:

```bash
./automation_status.sh
launchctl print "gui/$(id -u)/com.navnoor.substacktrades"
```

Inspect scheduled-run logs:

```bash
tail -n 100 "$HOME/Library/Logs/SubstackTrades/refresh.log"
tail -n 100 "$HOME/Library/Logs/SubstackTrades/refresh-error.log"
```

`refresh.sh` refuses to ingest from a dirty worktree and synchronizes with
`origin/main` using fast-forward-only semantics. Its production push is retried
three times for transient network failures. Every push to `main` runs the
regression suite, validates the tracked snapshot, builds a fresh immutable
Pages artifact—including the terminal, deferred archives, six-endpoint public
data bundle, and per-article share assets—and deploys it. Pull requests run the
same quality gate without production credentials or deployment. Production runs are
serialized and never cancelled midway; stale pull-request runs are cancelled.
The release is then fetched over HTTPS and checked against the exact commit,
record counts, and data checksum. Actions are restricted to GitHub-owned,
full-SHA-pinned dependencies, and `main` rejects force pushes, deletion, and
non-linear history while preserving the scheduled updater's normal direct push.

Local refreshes are transactional: new source data is built and validated in an
isolated candidate directory, the previous promoted snapshot is preserved, and
any regression-test, staging, or local-commit failure restores that snapshot.
A push failure retains the clean local commit for the next retry. A candidate
can therefore neither leak into the next scheduled run nor trigger a GitHub
Pages deployment unless its full local quality gate passes. GitHub Pages
then publishes the exact tested artifact atomically and the post-deploy smoke
test verifies HTTPS, revision, counts, snapshot checksum, the two independently
recorded deferred-asset hashes, the exact HTML hash, the complete six-endpoint
data bundle, and the discovery/social support assets before declaring it
healthy. Artifact validation separately proves complete catalogue-to-card/stub
coverage; the release checklist spot-checks representative pairs in production.
Deployment artifacts are retained for seven days. A separate least-privilege
watchdog is scheduled every four hours to rebuild the release fingerprints,
verify the exact published revision and public-data bundle, and reject a
research snapshot older than 16 hours, leaving margin above the longest
scheduled refresh interval.

Manually redeploy the current `main` snapshot without fetching publications:

```bash
gh workflow run update.yml --ref main
gh run list --workflow update.yml --limit 5
```

Only the scheduled Mac can discover new publication posts. A manual workflow
run rebuilds and redeploys the already tracked snapshot.

## Privacy-respecting measurement

The site intentionally ships no analytics SDK, tracking pixel, advertising
cookie, session replay, or background telemetry. Search text and tab-session
decision packets are not sent to this project. Explicit **Copy view** actions
may place the active research query in the copied URL so the user can choose to
share it. Maintainers can use GitHub's aggregate repository traffic window and
Google Search Console for discovery health without embedding reader tracking in
the terminal. Search Console ownership and sitemap submission are manual owner
steps documented in [LAUNCH_RUNBOOK.md](LAUNCH_RUNBOOK.md).

## Launch and incident operations

The complete preflight, deploy verification, rollback, monitoring, and incident
checklists live in [LAUNCH_RUNBOOK.md](LAUNCH_RUNBOOK.md). Treat a successful
post-deploy exact-release smoke test—not merely a green upload—as the release
boundary.

## Validate and preview locally

Run all regression tests and validate the complete tracked deployment snapshot.
This command works in a fresh clone because it does not depend on ignored local
publication caches:

```bash
python3 -m unittest discover -s . -p 'test_*.py' -v
python3 validate_pipeline.py \
  --articles articles_index.json \
  --trades trades_extracted.json \
  --manifest snapshot_manifest.json
ruff check *.py
mypy --cache-dir "${TMPDIR:-/tmp}/nrt-mypy-cache"
```

On the scheduled ingestion Mac, add
`--posts all_sources_posts.json` to perform the stricter validation that binds
the tracked snapshot back to the ignored full-source cache.

Build the ignored local preview and serve it at <http://localhost:8000>:

```bash
python3 build_site.py
python3 -m http.server 8000 --directory docs
```

If `automation_status.sh` reports `NOT LOADED`, enable the macOS background
item and rerun `./install_automation.sh`. If refresh reaches Git but cannot push,
verify credentials with `gh auth status`; any local commit still ahead of
`origin/main` is retried by the next successful refresh.
