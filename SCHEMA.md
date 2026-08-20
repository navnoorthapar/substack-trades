# Public data and share-asset contract

Navnoor Research Terminal publishes a static, machine-readable view of the same
validated research catalogue used by the consumer application. The production
base URL is:

```text
https://navnoorthapar.github.io/substack-trades/
```

There is no API server, authentication layer, query language, or write method.
Every endpoint is an immutable file inside one GitHub Pages release. Consumers
should fetch `data/manifest.json` first, treat its `dataset_version` as the
snapshot identity, and then fetch the other files from the same release.

## Contract and versioning policy

- JSON is UTF-8 and contains no `NaN`, infinity, duplicate object keys, or
  symbolic-link indirection.
- The six `data/` files are generated together and validated as one bundle.
- Object fields are additive-only within a schema version. Consumers must
  ignore unknown fields. Removing or renaming a field, changing its type or
  meaning, or changing an identity rule requires a schema-version increment.
- `manifest.schema_version` versions the public data-layer contract.
  `brief.schema_version` independently versions the bounded article-brief
  structure. The terminal's compact `article_catalog.json` bootstrap asset has
  a separate `ARTICLE_WIRE_SCHEMA_VERSION`; version 3 requires the three body-revision
  provenance fields plus `publication_access` (`public`, `member`, or
  `unknown`) and the bounded `member_preview_chars` count, and hydrates them
  unchanged into every runtime article.
- `manifest.dataset_version` is the lowercase SHA-256 snapshot checksum from
  `snapshot_manifest.json`. It identifies the exact tracked article and
  observation snapshot, not an individual article and not a durable database
  revision.
- `manifest.generated_at` is the UTC snapshot-check timestamp, ending in `Z`.
  It is deliberately derived from the validated snapshot rather than the wall
  clock of a later rebuild.
- `source` plus `slug` is the cross-file article identity. Slugs are globally
  unique in a release. A source-native identity is `source` plus `source_id`.
- Integer positions in `search_index.json` are snapshot-local. They refer only
  to the `articles` array in that same `search_index.json`; insertions in a
  later snapshot can change every later position. Never persist an integer
  position as an article identity or reuse it across `dataset_version` values.
- Dates are ISO `YYYY-MM-DD` values or timezone-qualified ISO timestamps.
  Consumers must not infer a publication time when only a date is supplied.
- All canonical and alternate URLs are HTTPS URLs without credentials,
  fragments, or custom ports.
- Absence of an entity, relationship, family-specific signal, brief section,
  or observation is not evidence that the full source lacks it. The generators
  deliberately prefer precision and may abstain.

## Endpoint summary

| Endpoint | Shape | Purpose |
|---|---|---|
| `data/articles_index.json` | article array | Complete public catalogue and bounded authored briefs |
| `data/latest.json` | 20-row article projection | Small newest-publication feed |
| `data/manifest.json` | object | Version, freshness, counts, and endpoint discovery |
| `data/search_index.json` | inverted index plus article rows | Fast entity/topic lookup and deduplication |
| `data/related.json` | article-keyed adjacency lists | One to five explainable self-link candidates per article |
| `data/families.json` | family-to-slug object | Deterministic topic-family partition |

## Terminal bootstrap asset

`article_catalog.json` is an application bootstrap asset rather than a seventh
public-data endpoint. Its top-level object has exactly four keys:

| Field | Type | Guarantee |
|---|---|---|
| `schema_version` | integer | Exactly `1` for this envelope |
| `article_wire_schema_version` | integer | Must equal the terminal's supported compact article schema |
| `data_checksum` | lowercase SHA-256 | Must equal the tracked snapshot checksum embedded in the same HTML shell |
| `articles` | array | Exact compact projection of every body-backed research article, with unique `a_` plus 14-lowercase-hex runtime IDs |

The tested HTML binds the exact asset bytes by SHA-256 and the expected article
count. The client installs zero catalogue rows until the response is same-origin
HTTP success and its digest, exact envelope, wire version, snapshot checksum,
count, and identities all validate. This asset is capped separately from the
first-load HTML and remains inside the aggregate Pages artifact budget.

The endpoint list in `manifest.json` is authoritative. Every entry is relative
to the project base URL. Resolve entries with a standards-compliant URL resolver
against `https://navnoorthapar.github.io/substack-trades/` (not against the
site origin alone and not against the manifest file URL). For example,
`urljoin(PROJECT_BASE, "data/latest.json")` remains inside `substack-trades/`.

## `data/articles_index.json`

This file is byte-for-byte equal to the tracked `articles_index.json`. It is a
JSON array in deterministic catalogue order. Every object has these fields:

| Field | Type | Guarantee |
|---|---|---|
| `source` | string enum | Exactly `substack`, `medium`, `patreon`, or `fxempire` |
| `source_id` | non-empty string | Source-native identifier; unique with `source` |
| `slug` | non-empty string | Globally unique stable path key within the catalogue |
| `title` | non-empty string | Public source title |
| `subtitle` | string | Public subtitle, or `""` when none is retained |
| `post_date` | ISO string | Public publication date/timestamp |
| `url` | HTTPS URL string | Canonical public source URL |
| `audience` | string | Public access/audience label supplied by the source pipeline |
| `wordcount` | non-negative integer | Source/pipeline body count used for public full-body coverage; `0` for registry and member-access rows, so it is not a member-preview length |
| `content_status` | string enum | `full`, `excerpt`, or `registry` |
| `family` | string enum | Exactly one topic family described below |
| `brief` | object | Bounded, source-verifiable brief described below |

Every Substack and Medium row also has these body-revision provenance fields:

| Field | Type | Guarantee |
|---|---|---|
| `body_revision_status` | string enum | `current`, `prior`, or `unverified` |
| `source_updated_at` | string | Source revision attributed to the captured body, or `""` when the source exposes no revision timestamp |
| `observed_source_updated_at` | string | Source revision observed by the refresh, or `""` when the live source exposes no revision timestamp |

`current` requires the two revision timestamps to match. They may both be empty
only for an excerpt from a live source, such as RSS, that exposes no separate
update timestamp. `prior` requires two non-empty, unequal timestamps and is
always an excerpt. `unverified` is always an excerpt and means a cached capture
remains searchable but its body revision was not verified during the current
refresh; either timestamp may be unavailable. Only `current` may be labelled
`full`, and a `full` row requires two non-empty matching revision timestamps, a
positive declared word count, and a non-empty captured-body digest. These
fields describe captured-source provenance, not whether the article's claims
are correct or current in the market.

If a public Substack list row exposes more than one exact preview surface,
terminal truncation markers are ignored only for compatibility comparison.
Compatible prefix/subset variants use the longest bounded exact surface;
incompatible variants remain source-degraded but still select the same longest
surface deterministically. A newly changed surface cannot reuse stale cached
text. Once that exact current excerpt and its source revision are persisted, an
identical later observation is an article-level coverage notice rather than a
continuing source-discovery failure.

The following other additive fields may be present:

| Field | Type | Guarantee |
|---|---|---|
| `alternate_urls` | object | Other source name to canonical HTTPS twin URL; never repeats the row's own source |
| `access` | `"public"` or `"paid"` | Patreon catalogue accessibility for an anonymous viewer; not a price, pledge, or subscriber field |
| `member_preview` | object | Required only for member-access Substack/Medium rows; exact anonymous preview proof described below |

Substack and Medium entries have `content_status` `full` or `excerpt`. Patreon
and FX Empire entries are metadata-only and always have `content_status`
`registry`, `wordcount` `0`, no republished body, and no body-revision
provenance fields.

### Member-preview object

A Substack `audience: only_paid` or Medium `audience: locked` row must be
`content_status: excerpt` and carry exactly:

| Field | Type | Guarantee |
|---|---|---|
| `schema_version` | integer | Exactly `1` |
| `surface` | string enum | Substack: `anonymous-substack-list` or `metadata-only`; Medium: `anonymous-medium-profile` or `metadata-only` |
| `text` | string | Deterministic bounded excerpt derived only from anonymous preview text, with a maximum length of 1,200 characters including any truncation ellipsis, or `""` for metadata-only |
| `character_count` | integer | Exactly the length of `text`, in `0..1200` |
| `body_sha256` | SHA-256 string | Digest of the exact UTF-8 `text` bytes |

A zero count requires the `metadata-only` surface, the empty-body digest, and a
brief with no lead, fallback evidence, sections, or checkpoints. A non-zero
count requires a non-metadata surface and a non-empty digest. The brief's
`body_sha256`, every brief span, and any member-derived observation must bind to
that exact preview. Locked Medium subtitles are empty unless the subtitle is an
exact substring of the proven preview. Non-member rows must not carry
`member_preview`; source collectors reject unsupported Substack audience or
Medium visibility enumeration values rather than assigning access by guesswork.

The embedded terminal derives `publication_access` conservatively from the raw
source label: Substack `everyone` is `public` and `only_paid` is `member`;
Medium `public` is `public` and `locked` is `member`. A validated source surface
without an access flag, such as a Medium RSS fallback, is `unknown`; collectors
reject unrecognized enumeration values rather than guessing. The runtime
`member_preview_chars` is the exact `member_preview.character_count`. Therefore
`content_status: excerpt` alone does not prove that preview text exists;
consumers must inspect the proof/count.

Medium RSS is treated only as a bounded incremental surface. A healthy
`complete_archive` mode requires two identical complete legacy-archive passes
and two identical RSS passes, followed by an exact match between all ten RSS
IDs/publication instants and the archive's newest ordered edge. A stable but
stale archive therefore cannot conceal a newer supported-RSS head; its already
fetched RSS window is evaluated by the fallback lineage rules instead of being
fetched again. If RSS is unavailable, the alleged archive is not published.

A healthy
`validated_history_plus_current_rss` source status requires two identical
normalized ten-row RSS windows for established history, a non-regressing newest
timestamp, exact publication timestamps for known IDs, and a contiguous overlap
in the newest validated-history order. It does not assert a newly enumerated
complete archive: rows outside the RSS window remain `body_revision_status:
unverified`. A missing overlap, an unknown row below the first overlap, an
incomplete window, timestamp drift, or a changing window uses
`trusted_history_rss_gap_quarantined`; the trusted catalogue remains byte-for-
byte equivalent when it is already the output, or exactly equal as JSON data in
an isolated transaction candidate, and the unproven merge cannot become a
future lineage input.

The tracked `medium_profile_sequence_bridge.json` object has exactly these
fields: `schema_version`, `source`, `author_username`, `surface`, `profile_url`,
`reviewed_at`, `expires_at`, `rss_window_ids`, and
`previous_history_prefix_ids`. The two ID arrays contain exactly ten and two
unique lowercase 12-hex Medium IDs, respectively, and must not overlap. The
surface is `operator-reviewed-direct-public-profile-sequence`; author and URL
are fixed to this catalogue. Review/expiry values are canonical UTC-second
instants, expiry must follow review by no more than three days, the full live RSS
order must equal `rss_window_ids`, and the newest trusted-history prefix must
equal `previous_history_prefix_ids`. A successful bridge emits
`operator_reviewed_profile_bridge_plus_current_rss` and carries those bounded
review facts first in fetch-status `provenance`, then unchanged in
`snapshot_manifest.json` at `sources.medium.provenance`. Both the manifest
writer and validator require the exact provenance key set, fixed surface and
profile URL, canonical UTC-second review/expiry instants, a source check inside
that no-more-than-three-day window, and exactly ten plus two unique lowercase
IDs with no overlap. Provenance is forbidden on every unrelated source mode. A
different future edge cannot reuse the record because either the RSS sequence
or the post-merge history prefix no longer matches.

An observation derived from a member preview must carry
`source_body_sha256` equal to that preview's digest; a non-member observation
must not carry the field. This binds the tracked observation and the deferred
public observation asset to the same bounded source bytes.

### Brief object

Every `brief` contains:

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | positive integer | Version of the brief structure |
| `body_sha256` | 64-character lowercase hex string | Digest of the captured source body, including the empty body used by registry records |
| `lead` | span object or `null` | Bounded authored lead |
| `sections` | array | Zero or more high-precision authored sections |
| `fallback_evidence` | span object or `null` | Exact evidence span used when structured coverage is sparse |
| `checkpoints` | array | Zero or more explicit dated public checkpoints |

A normal span carries `text`, `start`, `end`, `sha256`, and `truncated`.
Section rows additionally carry `kind`, `heading`, and `source_order`.
Checkpoint rows additionally carry `date`, `date_label`, and `context_kind`.
Offsets and hashes bind displayed text to the captured body revision. They do
not prove that a `prior` or `unverified` capture matches the source's current
text. `truncated: true` must be respected; it is not permission to infer the
omitted text.

## `data/latest.json`

This endpoint is an array containing exactly the newest 20 catalogue entries,
or the whole catalogue if it contains fewer than 20. Rows are sorted by parsed
publication instant, source, and slug, all descending. Each row contains
exactly:

| Field | Type |
|---|---|
| `source` | string |
| `slug` | string |
| `title` | string |
| `subtitle` | string |
| `post_date` | ISO string |
| `url` | HTTPS URL string |
| `alternate_urls` | object |

This is the preferred polling surface for a downstream planner that only needs
to detect newly published items. It is a projection, not a second source of
truth; resolve complete metadata through `articles_index.json`.

## `data/manifest.json`

The manifest contains exactly:

| Field | Type | Guarantee |
|---|---|---|
| `schema_version` | integer | Current public data-contract version |
| `dataset_version` | SHA-256 string | Exact validated snapshot checksum |
| `generated_at` | UTC timestamp string | Snapshot check time; validated against future skew and a 16-hour freshness policy |
| `article_count` | non-negative integer | Equals the length of `articles_index.json` |
| `source_counts` | object | Exact count for all four sources; every count is greater than zero in a deployable release |
| `family_counts` | object | Count for each of the seven allowed families |
| `endpoints` | string array | Complete sorted list of all six project-relative `data/` paths |

Counts are release-integrity assertions, not readership, subscriber, position,
performance, or revenue statistics.

## `data/search_index.json`

This file contains exactly two top-level fields:

```json
{
  "entities": {"citadel": [6, 14], "gamma": [53, 60]},
  "articles": [
    {
      "slug": "example-slug",
      "source": "substack",
      "title": "Example title",
      "post_date": "2026-01-01T00:00:00Z",
      "url": "https://example.invalid/article",
      "entities": ["citadel", "gamma"]
    }
  ]
}
```

`articles` preserves the master catalogue order. Each row contains exactly
`slug`, `source`, `title`, `post_date`, `url`, and `entities`. `entities` is a
lexicographically sorted array of unique normalized terms. The top-level
`entities` object is lexicographically ordered; every value is the unique,
ascending list of snapshot-local positions whose article row contains that
term. The two representations are validated as exact inverses.

Terms match `^[a-z0-9]+(?:-[a-z0-9]+)*$`. Normalization applies Unicode NFKC,
removes possessives before ASCII folding, maps supported Greek symbols to names,
case-folds, tokenizes alphanumerically, and joins tokens with hyphens. Examples
include `D.E. Shaw` → `d-e-shaw`, `Hull-White’s` → `hull-white`, and `Γ` →
`gamma`. Curated aliases unify important firms, institutions, instruments,
tickers, models, and mechanisms. Unknown capitalized names are admitted only by
a conservative organization-suffix rule. Standalone uppercase strings are not
treated as tickers unless curated.

Extraction reads the title, non-boilerplate subtitle, brief lead, and brief
section headings/text. It deliberately excludes promotional subtitles and does
not use an ML service. The output is deterministic and must remain smaller than
500,000 bytes. Precision takes priority over recall; consumers should use it to
find overlap candidates, not to prove that a source does or does not mention a
term.

## `data/related.json`

This endpoint is an object whose keys are every `source:slug` identity in master
catalogue order. Every value contains one to five distinct, non-self rows. A
sparse article remains below five rather than receiving an ungrounded filler
link:

| Field | Type | Guarantee |
|---|---|---|
| `slug` | string | Target article slug |
| `source` | string | Target article source |
| `title` | string | Exact target title from the master index |
| `url` | HTTPS URL string | Exact target canonical URL |
| `score` | float | Finite value in `(0, 1]`, rounded to at most six decimals |
| `why` | array of 1–3 strings | Unique truthful explanations in the form `shared: normalized-term` |

Rows sort by descending score and then ascending `source:slug`. The ranking is
hand-rolled, standard-library TF-IDF: normalized unigrams and bigrams, sublinear
term frequency, smoothed inverse document frequency, common/unique-term filters,
and separately normalized title, subtitle, and brief vectors. The final weights
are title `0.45`, subtitle `0.15`, brief `0.25`, and IDF-weighted shared D2
entities `0.15`; the final score is capped at `1`. Brief text includes lead,
sections, fallback evidence, and checkpoints; promotional subtitles remain
excluded.

Shared D2 entities are preferred as explanations, followed by the
highest-contributing normalized terms that actually occur in both TF-IDF
vectors. For a sparse record with no same-field vector or entity overlap, an
exact normalized term found across different authored fields may provide a
low-weight coverage floor capped at `0.01`. It may complete up to five
deterministic candidates without treating a publication channel or a broad
taxonomy family as article evidence. Validation independently proves every
reason against the actual entity or textual features of both rows. The score is
a relative editorial-ranking signal; it is not expected return, confidence,
conviction, quality, suitability, or a recommendation.

## `data/families.json`

The endpoint contains exactly these keys, each mapping to an array of unique
slugs in master-catalogue order:

- `firm-mechanics` — how a named desk or fund makes money or is structured.
- `career-structure` — careers, compensation, pod economics, or organization.
- `model-critique` — a named model limitation, error, or proposed fix.
- `scandal-enforcement` — manipulation, fraud, court, or regulatory action.
- `event-reaction` — geopolitical, policy, election, or macro-event reaction.
- `market-structure` — venues, exchanges, clearing, regulation, or market
  mechanics.
- `other` — safe abstention when no precision rule wins.

Every article appears exactly once. Classification is deterministic and uses a
documented precedence: explicit misconduct/enforcement, career structure,
title-level named-model critique, title-level market structure, title-level
event reaction, named-firm mechanics, then narrow context fallbacks for career,
model critique, and market structure. Everything else is `other`. It does not
emit a probability or confidence score and must not be interpreted as one.

## Four-source archive and registry policy

Substack and Medium provide the content-bearing archive. Cross-posts are
collapsed conservatively and represented through `alternate_urls`. Patreon and
FX Empire extend catalogue coverage through public metadata registries:

- `patreon_registry.json` rows contain exactly `source_id`, `title`, `url`,
  `post_date`, and `access`. `access` is only `public` or `paid`, based on what
  an anonymous viewer can open. The collector never persists a body, teaser,
  pledge threshold, engagement count, subscriber count, or revenue field. A
  failed refresh retains a previously validated complete cache; without one it
  fails closed.
- `fxempire_registry.json` is manually maintained because it is a byline
  registry. Rows contain exactly `source_id`, `title`, `url`, and `post_date`.
  To update it, add the canonical FX Empire article URL, its numeric URL suffix
  as `source_id`, the public title, and ISO publication date; keep newest-first
  order, then run the full tests and tracked-data validation before publishing.

Registry twins are matched to content-bearing entries only by normalized title
within seven days, a strict title-similarity threshold, or an explicit reviewed
decision in `registry_crosslink_overrides.json`. Ambiguity retains a distinct
metadata row rather than silently merging it. Source preference is Substack,
then Medium, Patreon, and FX Empire; displaced or unmatched records remain in
the catalogue. No Patreon or FX Empire body is scraped or republished.

## Share cards and article stubs

For every globally unique article slug, the Pages artifact also contains:

- `/cards/<slug>.png` — a generated 1200×630 PNG containing the bounded title,
  source badge, publication date, and Navnoor Research wordmark.
- `/a/<slug>.html` — a lightweight static document with article-specific Open
  Graph and Twitter metadata, a canonical stub URL, and a redirect to the
  matching hash-selected dossier in the consumer terminal for content-bearing
  rows, or directly to the original public source for registry-only rows.

The stub exists because social crawlers do not execute the terminal's hash
routing. Stub URLs are included in the sitemap. Cards and stubs are additive;
they do not alter the existing consumer application, execute publication text,
or expose non-public data. A release is invalid if any catalogue slug lacks its
card/stub pair, if an extra pair has no catalogue owner, or if a stub references
the wrong title, image, or article route.

## Privacy policy enforced by the contract

> C3. PRIVACY RULE (absolute): this is a PUBLIC repo and PUBLIC site. NEVER add private analytics — no email open rates, subscriber counts, revenue, pledges, or dashboard-derived numbers. Only content metadata and already-public information (public reaction/comment counts are acceptable ONLY if already collected; do not build new private-data collection).

The build validator recursively rejects forbidden private-analytics keys from
every `data/` file. The data layer contains no reader identifier, search log,
Research Task, cookie, tracking pixel, or behavioral event. Member-source
body text is limited to the bounded anonymous preview proof above; no
authenticated or legacy cached member body belongs in a current release.
Downloading a static endpoint does not authorize downstream systems to enrich
it with private
creator-dashboard or reader-level data. Earlier Git commits and retained release
artifacts are outside this mutable endpoint contract and are tracked separately
as unresolved `LAUNCH-058` in [ISSUES.md](ISSUES.md).

## Local Research Task interchange

Research Tasks are not a public `/data/` endpoint. Their plaintext export and
tab-session storage use `schema_version: 3`, a bounded list of at most 250
items, and a required `source_snapshot` for each item. The snapshot retains the
observation and article IDs, exact displayed passage, public title/URL/date,
source, access and revision labels, parsed direction/instruments/underlying,
the three capture-time review flags plus their verification marker, and release
checksum. All three flags must be booleans before `review_flags_verified` can be
true; otherwise every flag is `null`/unavailable rather than falsely clean.
Missing or unsafe snapshots fail closed; the consumer must never substitute
current release text. `legacy_bookmark_migration: true` means an ID-only legacy
bookmark received its snapshot from the active release during migration, not
from the historical bookmark date.

Human fields are limited to task status/priority, owner, review date, next
action, hypothesis, contrary evidence, independent public source, numeric claim
context, catalyst, horizon, falsifier, tags, and memo. The only attestation keys
are `context_reviewed`, `public_source_recorded`, `numeric_traced`,
`contrary_recorded`, `falsifier_recorded`, and `claims_scope_reviewed`. A true
attestation requires its matching valid timestamp. Canonical normalization of
session data, imports, backups, and rollback payloads discards retired
confidence, position/entry, payoff, implementation, portfolio, live-risk, and
legacy attestation fields instead of reinterpreting them.

All task-facing display, filters, date ranges, ordering, links, shortcuts,
citations, copies, and CSV exports are derived from the retained snapshot. A
separate comparison may report current-release drift, but cannot replace the
retained evidence. Orphaned tasks remain visible up to the bounded 250-item
limit and participate in the same search, filter, sort, edit, archive, copy,
shortcut, and CSV paths. A newer imported task with the same ID but a different
normalized `source_snapshot` is a retained-source conflict and requires a
separate explicit confirmation before the ordinary import preview.

## Consumer workflow

1. Fetch `manifest.json`; reject an unsupported `schema_version` or stale
   `generated_at` according to the consumer's policy.
2. If `dataset_version` is unchanged, no tracked publication snapshot changed.
3. Fetch `latest.json` for intake, then resolve complete rows through
   `articles_index.json`.
4. Use `search_index.json` to detect topic/entity overlap. Translate integer
   positions immediately to `source:slug` and discard the positions afterward.
5. Use `related.json` as an explainable self-link candidate set and retain human
   editorial review.
6. Use `families.json` for coverage planning; do not interpret `other` as low
   quality or any family as a recommendation.

Example:

```bash
BASE='https://navnoorthapar.github.io/substack-trades'
curl --fail --silent --show-error "$BASE/data/manifest.json" | python3 -m json.tool
curl --fail --silent --show-error "$BASE/data/latest.json" | python3 -m json.tool
curl --fail --silent --show-error "$BASE/data/search_index.json" > /tmp/nrt-search.json
```
