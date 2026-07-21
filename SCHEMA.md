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
  structure.
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
| `data/related.json` | article-keyed adjacency lists | Five explainable self-link candidates per article |
| `data/families.json` | family-to-slug object | Deterministic topic-family partition |

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
| `wordcount` | non-negative integer | Captured body word count; `0` for registry-only records |
| `content_status` | string enum | `full`, `excerpt`, or `registry` |
| `family` | string enum | Exactly one topic family described below |
| `brief` | object | Bounded, source-verifiable brief described below |

The following additive fields may be present:

| Field | Type | Guarantee |
|---|---|---|
| `alternate_urls` | object | Other source name to canonical HTTPS twin URL; never repeats the row's own source |
| `access` | `"public"` or `"paid"` | Patreon catalogue accessibility for an anonymous viewer; not a price, pledge, or subscriber field |

Substack and Medium entries have `content_status` `full` or `excerpt`. Patreon
and FX Empire entries are metadata-only and always have `content_status`
`registry`, `wordcount` `0`, and no republished body.

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
Offsets and hashes bind displayed text to the captured source. `truncated: true`
must be respected; it is not permission to infer the omitted text.

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
catalogue order. Every value contains exactly five distinct, non-self rows:

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
low-weight coverage floor capped at `0.01`. This completes five deterministic
candidates without treating a publication channel or a broad taxonomy family
as article evidence. Validation independently proves every reason against the
actual entity or textual features of both rows. The score is a relative
editorial-ranking signal; it is not expected return, confidence, conviction,
quality, suitability, or a recommendation.

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
decision packet, cookie, tracking pixel, or behavioral event. Downloading a
static endpoint does not authorize downstream systems to enrich it with private
creator-dashboard or reader-level data.

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
