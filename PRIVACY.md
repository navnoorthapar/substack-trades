# Privacy

Last updated: 2026-08-11

Navnoor Research Archive is a static, public research-index website. It is
designed to work without an account and without collecting reader data for the
project owner.

## What the site does not collect

The published application includes no analytics SDK, advertising pixel,
tracking cookie, session replay, account system, form submission, or background
telemetry. Search text, the local research question, the deterministic search
subject, filters, reading activity, and Local Review entries are not
transmitted to this project.

The archive may show a contextual link to an article on its original
publication and, for Substack member-access previews, a separate link to the
publication's subscription page. Passage Search repeats those two
choices once inside an expanded paid source note so the public evidence remains
useful before any off-site continuation. Those links contain no reader question,
search subject, filter, selected entity, article state, referral, or Local Review
value. The external site receives a request only after the reader chooses
the link and applies its own terms and privacy practices.

GitHub Pages necessarily serves the static files and may process ordinary web
request metadata under GitHub's own terms and privacy practices. Maintainers may
view GitHub's aggregate, limited-window repository traffic statistics. Google
Search Console may be used to understand search discovery without adding a
tracking script to the application.

## Public archive and data endpoints

The site publishes a six-file, machine-readable `/data/` bundle containing the
same public research catalogue used by the archive. It includes public source
metadata, bounded captured Substack/Medium research, deterministic topic and
related-article indexes, and integrity/freshness counts. Article-specific social
cards and crawler stubs contain bounded public title, source, publication date,
route metadata, and (in the stub description) the retained public subtitle or a
generic fallback. All of these files are public and may be cached by browsers,
search engines, GitHub Pages, and downstream consumers.

A member-access Substack or Medium row publishes a `member_preview` proof. It
contains either a deterministic bounded excerpt derived only from text visible
on a validated anonymous source surface, capped at 1,200 characters, or an empty
`metadata-only` state. Its recorded length and SHA-256 digest must match the
published excerpt, and its brief and any derived observation are bound to the
same digest. The current pipeline does not publish authenticated, legacy cached,
or otherwise private member-body text. A locked Medium row without a trusted
anonymous proof becomes metadata-only. Collectors reject unrecognized source
enumerations; a validated surface without an access flag remains `unknown`
instead of being guessed.

Patreon and FX Empire are metadata-only registry sources. Patreon records may
state whether an anonymous visitor sees the item as `public` or `paid`; the
project does not persist or publish the article body, teaser, pledge amount,
subscriber count, revenue, or creator-dashboard data. FX Empire records are
manually reviewed public byline metadata and do not include article bodies.

> C3. PRIVACY RULE (absolute): this is a PUBLIC repo and PUBLIC site. NEVER add private analytics — no email open rates, subscriber counts, revenue, pledges, or dashboard-derived numbers. Only content metadata and already-public information (public reaction/comment counts are acceptable ONLY if already collected; do not build new private-data collection).

The public data validator rejects forbidden private-analytics keys recursively.
The data bundle does not contain reader identities, search history, Local
Review entries, cookies, or behavioral events. The endpoint contract is documented in
[SCHEMA.md](SCHEMA.md).

## Historical publication boundary

The 2026-08-08 candidate snapshot has been sanitized to the exact anonymous
preview/metadata boundary described above. Earlier public Git commits and
retained Pages or rollback artifacts included member-source brief text and
derived observations under the older capture policy. Replacing the current
snapshot does not erase immutable Git history, copies, caches, or retained
artifacts. A coordinated purge would be destructive, conflicts with the current
no-history-rewrite repository rule, cannot recall third-party copies, and
therefore requires explicit owner authorization. This unresolved boundary is
recorded as `LAUNCH-058` in [ISSUES.md](ISSUES.md).

## Data stored on the reader's device

Local Review uses plaintext `sessionStorage`, partitioned by origin and the
current top-level browser tab. It survives reloads but ends when that tab session
closes; other ordinary tabs do not receive the scratchpad. Review baselines and
display preferences use persistent functional browser storage. Exported Local
Review backups are plaintext. The editor is limited to human research framing,
public-source citations, and timestamped attestations; it does not solicit
confidence, position/entry, payoff,
execution/borrow/funding, portfolio-fit, or live-risk fields. Free-text fields
still must not contain confidential, personal, client, position, material
non-public, or regulated information.

The application asks for an acknowledgement before Local Review storage is first used.
A valid legacy queue left by the prior origin-wide implementation is moved into the tab
session and removed from persistent storage. Unreadable records fail closed and
can be preserved before destructive cleanup. Imports retain a tab-session
rollback across reloads. Valid session data, imports, normal backups, and
rollback payloads are canonicalized to the current bounded Local Review schema. Retired
confidence, position/entry, payoff, implementation, portfolio, live-risk, and
legacy attestation fields are discarded; old booleans are never reinterpreted
as new attestations, and an attestation is checked only when its matching
timestamp is valid. An item without a safe retained source snapshot is rejected
rather than rebound to current article content. The interface can clear both
the current tab items and accessible legacy queue keys; clearing browser site
data also removes them.

Local Review display, filtering, sorting, date windows, source opening, keyboard
shortcuts, copied citations, item text, and CSV exports use the retained source
snapshot. Current-release changes are shown only as an explicit comparison.
This prevents an updated extraction from silently changing the evidence behind
an existing item. Items whose current observation disappears remain fully
searchable, editable, archivable, copyable, and exportable. A newer same-ID
import with a different retained source anchor requires its own explicit
conflict approval before the ordinary import preview.

New item snapshots retain the capture-time negation, reference-line, and
truncation flags. Older or malformed payloads without a complete three-boolean
proof are labeled `review flags unavailable`; absence is never silently
interpreted as a clean capture. A migrated legacy ID-only bookmark is labeled
as an active-release snapshot created during migration, not evidence retained
from the historical bookmark date.

## Explicit sharing

The normal address bar does not persist global search text, Passage Search's
local research question, its literal all-term search subject, refinements, or
source-passage anchors. Ordinary Passage Search edits remain
in page memory. When a reader explicitly selects **Copy view**, the application
generates a share URL without mutating normal browser history or the active
address. That copied URL may include the research question, search subject,
refinements, optional Treasury-context toggle, and exact source anchor. Treasury
context is post-retrieval provenance and cannot alter evidence membership or
ordering. Treat the URL as public and use the feature only for non-confidential
research context. Exported Local Review backups and copied citations leave the browser
only when the reader chooses where to send or save them.

## Contact

For security-sensitive reports, follow [SECURITY.md](SECURITY.md). For other
questions, use the repository's public issue tracker without including private
or confidential information.
