# Navnoor Research Archive

This project collects authored research from `navnoorbawa.substack.com` and
`medium.com/@navnoorbawa`, adds privacy-safe public catalogue metadata from
Patreon and FX Empire, extracts structured fields from source passages, and
publishes a research archive at
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
  tracked snapshot -> validation + tests -> archive shell + verified article catalogue
                                         -> deferred article-record + observation JSON
                                         -> six versioned data/*.json endpoints
                                         -> article-specific /cards/*.png + /a/*.html
                                         -> robots/sitemap/manifest/favicon/social image
                                         -> one immutable Pages artifact
                                         -> atomic deployment + exact smoke test
```

Medium's legacy profile GraphQL archive is attempted for recovery but is not a
supported or currently dependable public interface. Its supported ten-item RSS
feed can extend a previous validated catalogue only with complete-window
lineage proof. A simultaneous archive and RSS outage fails closed and preserves
the last good snapshot. Both feeds bind every item to the exact canonical
Navnoor author URL;
the RSS collector accepts only Medium's documented-shaped tracking query and
stores the query-free canonical identity. GraphQL paragraph arrays have no
independent declared-length proof, so even public captures remain exact
`current` excerpts rather than being overstated as full articles. Cross-posts
are matched using Medium's explicit Substack notice, normalized titles,
subtitles plus dates, conservative similarity, and reviewed immutable
Medium-ID-to-Substack-slug mappings in `medium_dedupe_overrides.json`. The
human-readable titles in those mappings are cross-checks, not identities.
Substack remains the canonical card for cross-posts; only Medium-only articles
are added.

Two complete Substack pagination passes must produce the same ordered
source-field signatures before any per-article body lookup begins; overlapping
pages or a catalogue that changes between passes fail closed. A current body is
labeled `full` only when the source explicitly marks it public, the detail
revision exactly matches the list row, and the captured text covers at least
97% of the list endpoint's declared word count.
When a public list row exposes both a short preview and a longer HTML-derived
preview, compatible prefix/subset variants collapse to the longest bounded
exact surface deterministically. A changed surface replaces stale cached text
and records one degraded observation; an identical later surface is a disclosed
coverage limitation rather than a continuing discovery outage. Mutually
inconsistent current list surfaces stay degraded while retaining the same
deterministic bytes on every run.
Paid rows never trigger a detail-body request. Their tracked body is either a
deterministically bounded, hash-bound anonymous list preview of at most 1,200
characters or empty metadata-only state; authenticated and legacy cached member
bodies are excluded. Public detail recovery has a hard per-refresh request
budget. A private retry
timestamp rotates that budget across refreshes: new
rows run first, followed by never-attempted and then oldest-attempted rows, so a
fixed newest-first catalogue cannot permanently starve an older unresolved
article. Previously captured exact bodies may be retained for research
continuity only for source-public rows, but the tracked catalogue and archive
distinguish current, prior, and revision-unverified captures. Historical public
passages and their derived observations are visibly flagged, excluded from
high-context eligibility, and never labeled as current full text.

`all_posts.json` and `all_sources_posts.json` stay local. The tracked pipeline
state includes `medium_posts.json`, `patreon_registry.json`,
`fxempire_registry.json`, `registry_crosslink_overrides.json`,
`articles_index.json`, `trades_extracted.json`, `snapshot_manifest.json`, and
`.direction_cache.json`; retaining the catalogues prevents a temporary source
failure from erasing older articles. Patreon and FX Empire records are
metadata-only: the project does not scrape or republish their article bodies.
Production builds consume the validated article and observation snapshots. The
manifest binds exact input bytes, counts, publication freshness, and per-channel
fetch health. Every included source check must be no more than one hour behind
the manifest and no more than sixteen hours old at validation time, so a fresh
manifest cannot conceal stale upstream evidence. Deferred assets carry the same checksum and are rejected by the
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
validated four-source snapshot as the archive:

- [`data/articles_index.json`](https://navnoorthapar.github.io/substack-trades/data/articles_index.json) — the complete Substack, Medium, Patreon, and FX Empire catalogue with bounded briefs.
- [`data/latest.json`](https://navnoorthapar.github.io/substack-trades/data/latest.json) — the deterministic newest-20 projection.
- [`data/manifest.json`](https://navnoorthapar.github.io/substack-trades/data/manifest.json) — schema version, dataset identity, freshness, counts, and endpoint discovery.
- [`data/search_index.json`](https://navnoorthapar.github.io/substack-trades/data/search_index.json) — a compact deterministic entity/topic index.
- [`data/related.json`](https://navnoorthapar.github.io/substack-trades/data/related.json) — one to five explainable related-research candidates per article when exact overlap exists.
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
matching article record; registry-only stubs open the original public source.
The exact field, ranking, versioning, registry, privacy, and share-asset
contracts are in [SCHEMA.md](SCHEMA.md).

## Subscriber conversion

Member-access research is promoted as source completeness, never as a simulated
browser paywall. The owner-first home gives the current Substack subscription
terms the primary action and keeps the latest published note as a clearly
labelled preview path. The archive keeps public research usable, distinguishes what the source
permits from how much this release captured, and presents a contextual
continuation panel only for a canonical paid Substack record. In the evidence
workspace, the same boundary appears once inside an expanded paid source note,
at the point where the analyst has inspected its captured passage.
A non-empty panel passage is the exact anonymous source preview proven by the
snapshot; a row without such a preview is labeled metadata-only. Each panel
links to the exact article page and offers a separate no-tracking path to current
subscription terms. Locked Medium and paid Patreon records retain their own
source identity and never imply that a Substack subscription unlocks them.

The public data validator fails closed if a member-access Substack or Medium
record is marked as a full capture, if a member preview exceeds 1,200 characters,
if its length or digest is false, or if a brief/observation is not bound to that
exact preview. Prices, trials, subscriber counts, performance claims, artificial
urgency, and behavioral telemetry are not embedded. The complete editorial,
privacy, testing, current-snapshot, historical-exposure, and off-site
publication workflow is in
[SUBSCRIPTION_PLAYBOOK.md](SUBSCRIPTION_PLAYBOOK.md). Historical Git and retained
release-artifact handling remains the explicit launch blocker documented as
`LAUNCH-058` in [ISSUES.md](ISSUES.md).

## Product scope: published research archive

This is a published research index with source-linked passages and capture
provenance. It helps a reader discover published ideas, inspect the exact
supporting passage, triage uncertainty, and turn a candidate into a source-bound
local review item. It is deliberately
not a portfolio-management, order-management, risk, accounting, compliance, or
investor-reporting system.

That boundary and diligence structure are informed by
[CFA Institute's manager-selection framework](https://www.cfainstitute.org/insights/professional-learning/refresher-readings/2026/investment-manager-selection),
the [AIMA 2025 manager due-diligence questionnaire](https://www.aima.org/article/presenting-the-2025-edition.html),
[CFA Standard V(A)](https://www.cfainstitute.org/standards/professionals/code-ethics-standards/standards-of-practice-v-a),
[CFA Standard V(C)](https://www.cfainstitute.org/standards/professionals/code-ethics-standards/standards-of-practice-v-c),
and the [SEC investment-adviser marketing guide](https://www.sec.gov/resources-small-businesses/small-business-compliance-guides/investment-adviser-marketing).
Those references shape questions, evidence retention, and disclosure boundaries;
they do not certify the product or establish legal compliance.
The archive supports published-source discovery and pre-decision research. It
does not manufacture NAV/P&L, attribution, exposure, leverage, VaR, stress,
liquidity, funding, counterparty, capacity, execution, compliance, or investor
metrics without the connected books and records required to calculate them.

### Passage Search

Passage Search opens with an owner-first research home assembled only from the
validated release and local browser state: one product promise, one primary
subscriber path, one featured note plus three compact recent rows, buyer-facing
archive proof, one local archive search, progressively disclosed review tools,
and collapsed coverage for all four publication sources. Review-baseline state
stays inside progressively disclosed local tools; the expanded research preset
uses "Recent · 7 days" until an explicit baseline permits "New since last
review."
Source-adapter health is kept separate from clock-aware snapshot freshness, so
an old release cannot present itself as current merely because every adapter
was healthy when checked.

The owner home does not fetch the deferred observation archive. Passage
retrieval begins only when the reader submits its single search field. The
expanded evidence workspace then separates two responsibilities: **Search the
archive** is a deterministic literal all-material-term retrieval over the
published corpus, while the optional **Local review question** is browser-local
framing that never changes retrieval. Refinements remain closed until requested.
Passage Search organizes the corpus for diligence; it does not infer a house
view or turn a passage into a trade.

Primary evidence is clustered by source note, so repeated captured passages
from one article are not counted as independent sources. Every result reports
raw source-note and passage counts. A direct passage-level underlying or
all-term match can enter the primary ledger. Authored-headline matches,
structured-field context, and text-only mentions are shown in separate labeled
context sections and cannot become an evidence anchor merely because the
article title matched. The analyst must expand a source note and explicitly
anchor one exact displayed passage before opening a local review item; no
highest-ranked passage is selected on the analyst's behalf.

Automated extraction remains visibly provisional. Numeric and outcome/P&L
phrases are labeled as detected phrases; thesis language is a parser candidate.
Missing labels mean only that the bounded captured passage did not produce that
candidate, not that the complete source lacks the information. Truncation,
excerpt-only coverage, stale revisions, and review flags remain attached to the
source note and exact passage.

Optional macro context is provenance, not a portfolio signal. When enabled, it
uses an official observation dated on or before the article's publication
calendar date and labels that observation date. Daily observations are not
aligned to article timestamps and may post-date a same-day publication. The
reading is not an event, entry, position, or valuation date, does not establish
causality, and cannot change evidence membership or ordering. The Desk supplies
no holdings, live or position prices, P&L, sizing, exposure, liquidity
assessment, compliance determination, or recommendation. Those require the
firm's own books, market data, controls, and accountable human judgment.

The research question, search subject, refinements, and source anchor remain
in page memory during ordinary use and are not silently written to the address
bar. **Copy view** is the explicit sharing action: it generates a share URL from
the current Passage Search state without mutating normal browser history or the active
address. The copied URL may contain the non-confidential research question,
search subject, refinements, optional context toggle, and exact source anchor.

The Article Record is built around the article data:
the first eligible authored passage, contextual evidence, mechanism,
limitations, falsifiers, implementation, cited checkpoints, and exact source
provenance. Its evidence ledger keeps detected numeric tokens attached to their
original passage; tokens are lexical, deduplicated, capped, and never presented
as normalized or comparable facts. Duplicate spans are collapsed by source
identity, related research requires an exact mentioned-entity or underlying
overlap, and excerpt gaps are marked not assessable rather than absent.
Related Research adds a longitudinal view within each body-backed article. It
connect only repeated high-precision topics from the release search index,
identify whether each match came from the title, subtitle, opening, or a
classified section, retain full publication timestamps, show a bounded
seven-entry chronology, and compare captured research roles with the preceding
indexed publication. Exact older opening passages and their attached numeric
tokens load only on request. A capture
difference is an extraction fact—not evidence of a changed view,
contradiction, conviction, performance, or portfolio action.
The compact archive catalogue is a release-bound deferred bootstrap asset.
The browser starts no research UI until `article_catalog.json` passes same-origin
HTTP, exact SHA-256, wire-schema, snapshot-checksum, row-count, and unique-ID
verification. A missing or mismatched catalogue leaves an accessible recovery
screen with a bounded retry and release-status link; it never installs a partial
catalogue. This keeps the first-load HTML bounded as the archive grows without
weakening the exact-release contract.

Older article records are release-bound deferred assets. The browser rejects
missing, unknown, malformed, or hash-mismatched records before installing any
of them, and it never converts an unavailable record into a claim that
evidence is absent. Exact SHA-256 digests for the article catalogue and both
deferred research archives are embedded in the tested HTML, so swapped,
reordered, truncated, or otherwise altered asset bytes fail closed before JSON
installation. Deployment additionally requires a three-way match between each
independently recorded asset build digest, its HTML metadata binding, and the
bytes fetched from production. The complete HTML document is also matched
byte-for-byte to its tested build digest, and every generated inline script must
pass Node.js syntax validation before upload. The larger observation archive
loads only when a selected view or filter needs it, avoiding a late rerender of
the active article record.

The article-record navigation remains complete when the desktop rail collapses,
and the print/PDF layout preserves authored passages and public checkpoints
while removing tab-session Local Review fields.

Light and dark modes are two palettes of one private-research interface system.
They share the same information architecture, typography, spacing, control
geometry, selected-state indicators, semantic labels, focus treatment, and
contrast gates; only color and elevation change. A first visit follows the
operating-system preference before paint, while an explicit in-app choice is
retained across releases. Light uses warm paper and ink surfaces with midnight
navy actions; dark uses midnight navy and layered slate with pale institutional
blue actions. Restrained brass is reserved for editorial and premium detail,
while semantic states retain separate role-bound colors. Print/PDF output
always resolves to the tested light palette.

Parsed Passages and the Article Index provide fast passage-level review.
Directional labels describe parsed language, not an actor, verified position,
exposure, conviction, or current view. **Local Review** is a local,
non-confidential scratchpad for human diligence, not a scored approval queue. A
Local Review item retains the bounded source snapshot and dataset checksum, plus
analyst-authored hypothesis, contrary evidence, an independent public-source
citation, a key numeric claim with cited context, a public catalyst or
checkpoint, horizon, falsifier, owner, next action, review date, tags, memo, and
timestamped research attestations. It deliberately omits analyst-confidence scoring,
position or entry terms, payoff, execution/borrow/funding, portfolio-fit, and
live risk fields.

Article Record can begin that workflow without a detour: after the verified
observation archive loads, it displays every eligible passage in full and asks
the analyst to select one exact source anchor. The handoff validates article
ownership, refuses a conflicting imported retained anchor, and rolls back a
failed tab-session save. No passage is selected or truncated on the analyst's
behalf.

If a later extraction changes or removes the observation ID, the Local Review item
remains visible with its retained source snapshot instead of silently
disappearing. Local Review rows, filters, date ranges, sorting, keyboard/open actions,
citations, copies, and CSV exports remain bound to that retained snapshot; any
current-release difference is labeled separately. A missing or unsafe snapshot
fails closed rather than being replaced by current article text. Backup imports
merge with existing work, require separate approval before a newer same-ID item
can replace a different retained source anchor, and keep current-missing items
first-class in the same table/editor/export workflow. Removal archives an item
rather than destroying its history. "New since last review" advances only when the user explicitly
marks the review baseline; simply opening or reloading the site does not
acknowledge new research.

All passage-scored evidence fields are derived from the exact passage shown in
the inspector—never from hidden adjacent paragraphs. Mentioned-entity labels may
also come from the displayed article title and retain the original extracted
mention. Truncated captures carry an explicit flag. Direction classification
abstains when a passage negates or rejects a trade signal unless it subsequently
states an explicit affirmative position. These controls reduce false precision;
they do not replace reading the original article or obtaining independent
evidence.

Local Review uses plaintext `sessionStorage`, which is isolated to the
current top-level browser tab and survives reloads only until that tab session
closes. Explicit exports are plaintext backups. On first use, the archive
states these boundaries and prohibits confidential or regulated entries. A
valid legacy origin-wide queue is transactionally moved into the tab session
and removed from persistent storage; malformed legacy records fail closed and
can be preserved before cleanup. Valid saves, imports, and rollback payloads are
rewritten into the current bounded schema: retired position-like text,
confidence fields, and old attestation booleans are discarded rather than
silently reinterpreted. A checked attestation survives only with its matching
valid timestamp. Restore keeps a tab-scoped rollback across reloads. These
items are not an authenticated, shared, encrypted, or immutable enterprise
audit record. See [PRIVACY.md](PRIVACY.md) and [SECURITY.md](SECURITY.md).

Each new item snapshot stores the capture-time negation, reference-line, and
truncation flags with an explicit verification marker. Older or malformed
imports without complete boolean proof show `review flags unavailable` instead
of being treated as clean. Legacy ID-only bookmarks can be migrated, but the
interface labels their snapshot as captured from the active release during
migration—not as historical evidence from the bookmark date.

## Install the scheduled updater

```bash
./install_automation.sh
```

The installer copies the versioned LaunchAgent into
`~/Library/LaunchAgents`, installs the repository's versioned pre-push gate
without overwriting any conflicting hook setup, loads the updater, verifies it,
and starts one bounded refresh cycle. It then runs at **9:00 AM, 1:00 PM, and
10:00 PM local time** (09:00, 13:00, and 22:00) and once after login. Each
cycle makes at most three attempts: immediately, then after 15 minutes, then
after another 15 minutes. A success ends the cycle immediately. Three failures
leave the final nonzero exit visible to `launchctl` and `automation_status.sh`.

The retry budget lives in the foreground `scheduled_refresh.sh` supervisor;
the LaunchAgent deliberately does not use `KeepAlive/SuccessfulExit`, which
would relaunch a persistent fail-closed error forever. The existing
`refresh.sh` process lock and 30-minute successful-run guard remain in force,
so scheduled and manual runs cannot mutate the snapshot concurrently and a
recently completed refresh is still a cheap no-op. A manual run that finds a
live lock still exits cleanly. The scheduler alone asks that path to return the
temporary-failure code 75, so it waits 15 minutes and checks again: an incumbent
success then hits the recent-success guard, while an incumbent failure leaves
the next attempt free to perform the refresh. Lock ownership binds the PID to
its process start time, unchanged command, and physical repository working
directory. A dead owner or reused/foreign live PID is cleared instead of
blocking publication indefinitely.

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

The status command certifies only a settled current release. It exits nonzero
with wait-and-rerun guidance while a scheduled refresh/retry cycle, ingestion,
or deployment is still active, and it rejects a green workflow whose head SHA
does not equal remote `main`. It also exits nonzero for any source whose
validated status is explicitly degraded, naming the source, mode, streak start,
and consecutive refresh count.

Inspect scheduled-run logs:

```bash
tail -n 100 "$HOME/Library/Logs/SubstackTrades/refresh.log"
tail -n 100 "$HOME/Library/Logs/SubstackTrades/refresh-error.log"
```

Scheduled log entries label attempts `1/3` through `3/3`. Do not repeatedly
kickstart a permanently failing updater: first inspect the final error after
the bounded cycle. After correcting it, start a new bounded cycle with
`launchctl kickstart -k "gui/$(id -u)/com.navnoor.substacktrades"`.

`refresh.sh` refuses to ingest from a dirty worktree and synchronizes with
`origin/main` using fast-forward-only semantics. Its production push is retried
three times for transient network failures. Every push to `main` runs the
regression suite, validates the tracked snapshot, builds a fresh immutable
Pages artifact—including the archive, verified catalogue, deferred archives, six-endpoint public
data bundle, and per-article share assets—and deploys it. Pull requests run the
same quality gate without production credentials or deployment. Production runs are
serialized and never cancelled midway; stale pull-request runs are cancelled.
Production performs one bounded Pages attempt, then checks the exact live bytes
for late completion and freshly proves that the SHA still owns remote `main`;
it does not issue a blind same-SHA retry after a platform timeout. The release
is fetched over HTTPS and checked against the exact commit, record counts, and
data checksum. Actions are restricted to GitHub-owned,
full-SHA-pinned dependencies, and `main` rejects force pushes, deletion, and
non-linear history while preserving the scheduled updater's normal direct push.

Local refreshes are transactional: new source data is built and validated in an
isolated candidate directory, the previous promoted snapshot is preserved, and
the same complete Pages artifact policy used by CI runs before staging. Any
regression-test, release-artifact, staging, or local-commit failure restores
that snapshot.
The Medium collector reads its prior trusted catalogue and reviewed bridge only
from fixed script-root paths. It accepts no command-line path arguments or
filesystem-path environment overrides; `refresh.sh` runs the absolute script
with zero arguments from its private transaction directory, where the collector
writes only the fixed candidate and status filenames.
A push failure retains the clean local commit for the next retry. A candidate
can therefore neither leak into the next scheduled run nor trigger a GitHub
Pages deployment unless its full local quality gate passes. GitHub Pages
then publishes the exact tested artifact atomically and the post-deploy smoke
test verifies HTTPS, revision, counts, snapshot checksum, the verified catalogue,
the two independently recorded deferred-asset hashes, the exact HTML hash, the complete six-endpoint
data bundle, and the discovery/social support assets before declaring it
healthy. That support proof includes the deterministic archive-owned
`404.html`, an exact request to a nonexistent route that must retain HTTP 404
semantics without exposing GitHub's generic error UI, and the legacy `og.jpg`
compatibility alias. Artifact validation separately proves complete catalogue-to-card/stub
coverage; the release checklist spot-checks representative pairs in production.
Browser release-asset reads automatically retry only bounded transient network,
timeout, required-asset 404, 408/409/425/429, and 5xx failures. Invalid-request
or authorization responses and all integrity failures stay fail-closed. A
failed side-effect-free catalogue read can be retried in place;
once initialization begins, recovery requires the separate cache-busted clean
document reload so listeners and local state cannot be installed twice.
Deployable artifacts are retained for seven days. Each successful release also
retains a separately attested rollback bundle for 90 days: exact site bytes,
source JSON, tracked social image, release revision, all six fingerprints, and
an exact path-to-hash manifest for every published file. The original
successful `main` push or authenticated manual release proves the release
contract before archival. The manual emergency workflow then uses only current
trusted `main` tooling to authenticate that exact run and its latest-attempt
jobs, requiring ordered Pages-attempt, exact-live-smoke, post-deploy-authority,
and reconciliation evidence rather than accepting a green quality-only or
superseded run. It safely extracts and verifies the
schema-neutral attestation, deploys the archived site without executing
historical code, and fetches every live file over HTTPS for an exact
revision-cache-busted byte comparison. A separate
least-privilege watchdog is scheduled every four hours to rebuild the release
fingerprints, verify the exact published revision and public-data bundle, and
monitor both snapshot age and source-adapter health. A validated cached fallback
remains publishable so a transient upstream outage cannot delete research. Each
manifest carries `degraded_since` and `consecutive_degraded_checks`; the first
upgrade from an older manifest treats its last degraded source check as the first
provable observation. The watchdog warns during the first 48 continuous hours
and then fails until that source recovers, while exact-live verification still
runs independently. Snapshot age warns after 16 hours and fails after 36 hours,
leaving margin above the longest scheduled refresh interval without silently
accepting a stopped publisher.

Medium's supported public RSS is a bounded current window, not a complete
archive. `complete_archive` is emitted only after two matching legacy archive
passes are also checked against two matching RSS passes: all ten RSS IDs must be
the archive's newest edge in the same order and at the same publication
instants. A stale or inconsistent archive is never published as complete; the
already-fetched RSS window enters the same lineage-checked fallback used for an
archive outage, without a second RSS fetch. When the profile archive is
unavailable, the adapter reports healthy
incremental collection only after two exact normalized ten-row RSS passes agree,
the newest item does not regress, every known publication timestamp is exactly
unchanged, and a known-history overlap preserves the newest history order. A
missing overlap, an unknown item below the first overlap, an incomplete window,
or a changing RSS window is quarantined: the unproven merge never enters
`medium_posts.json`, and the prior trusted catalogue remains the next refresh's
input.

`medium_profile_sequence_bridge.json` is one transparent recovery exception for
the exact public profile sequence reviewed on 2026-08-20. Its exact-key schema
binds the complete ten-ID RSS window to the next two IDs observed on the direct
public profile, the expected newest two IDs in trusted history, the author and
profile URL, and a three-day maximum review lifetime. The adapter reports
`operator_reviewed_profile_bridge_plus_current_rss` plus the reviewed surface,
timestamps, and IDs when it is used. The manifest writer retains that exact
object at `sources.medium.provenance`; both the writer and release validator
reject missing/extra keys, invalid identity or time bounds, malformed ID
windows, and provenance attached to any unrelated mode. Any changed RSS
ID/order, changed trusted history prefix, future review, or expiry fails closed.
After one accepted merge, the trusted-history prefix changes, so the same
bridge cannot approve a later unrelated gap. Historical rows absent from RSS
remain revision-unverified in every successful incremental mode. Do not
hand-edit `medium_posts.json` to bypass these lineage checks.

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
Local Review contents are not sent to this project. Explicit **Copy view** actions may
place the active non-confidential Desk framing in the copied URL so the user can
choose to share it; generating that URL does not change the normal address-bar
history. Maintainers can use GitHub's aggregate repository traffic window and
Google Search Console for discovery health without embedding reader tracking in
the archive. Search Console ownership and sitemap submission are manual owner
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
PREVIEW_DIR="$(mktemp -d)"
SITE_OUTPUT_DIR="$PREVIEW_DIR" SITE_REVISION=local-audit python3 build_site.py
python3 validate_release.py \
  --site "$PREVIEW_DIR" \
  --articles articles_index.json \
  --trades trades_extracted.json \
  --manifest snapshot_manifest.json \
  --expected-revision local-audit
rm -r "$PREVIEW_DIR"
```

On the scheduled ingestion Mac, add
`--posts all_sources_posts.json` to perform the stricter validation that binds
the tracked snapshot back to the ignored full-source cache.

Build the ignored local preview and serve it at <http://localhost:8000>:

```bash
python3 build_site.py
python3 -m http.server 8000 --directory docs
```

For a clean committed release, run
`./release_gate.sh "$(git rev-parse HEAD)"`. A configured `.githooks/pre-push`
repeats that full gate from a detached worktree at the exact outgoing `main`
SHA, so uncommitted files cannot make a bad commit appear safe.

If `automation_status.sh` reports `NOT LOADED`, enable the macOS background
item and rerun `./install_automation.sh`. If refresh reaches Git but cannot push,
verify credentials with `gh auth status`; any local commit still ahead of
`origin/main` is retried by the next successful refresh.
