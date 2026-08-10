# Privacy

Last updated: 2026-08-11

Navnoor Research Terminal is a static, public research-intake website. It is
designed to work without an account and without collecting reader data for the
project owner.

## What the site does not collect

The published application includes no analytics SDK, advertising pixel,
tracking cookie, session replay, account system, form submission, or background
telemetry. Search text, filters, reading activity, and decision-workflow entries
are not transmitted to this project.

The terminal may show a contextual link to an article on its original
publication and, for Substack member-access previews, a separate link to the
publication's subscription page. Those links contain no reader query, filter,
selected entity, article-state, referral, or decision-workflow value. The
external site receives a request only after the reader chooses the link and
applies its own terms and privacy practices.

GitHub Pages necessarily serves the static files and may process ordinary web
request metadata under GitHub's own terms and privacy practices. Maintainers may
view GitHub's aggregate, limited-window repository traffic statistics. Google
Search Console may be used to understand search discovery without adding a
tracking script to the application.

## Public archive and data endpoints

The site publishes a six-file, machine-readable `/data/` bundle containing the
same public research catalogue used by the terminal. It includes public source
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
The data bundle does not contain reader identities, search history, decision
packets, cookies, or behavioral events. The endpoint contract is documented in
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

Decision Workflow packets use plaintext `sessionStorage`, partitioned by origin
and the current top-level browser tab. They survive reloads but end when that tab
session closes; other ordinary tabs do not receive the queue. Review baselines
and display preferences use persistent functional browser storage. Exported
queue backups are plaintext. Do not enter confidential, personal, client,
position, material non-public, or regulated information.

The application asks for an acknowledgement before workflow storage is first
used. A valid queue left by the prior origin-wide implementation is moved into
the tab session and removed from persistent storage. Unreadable records fail
closed and can be preserved before destructive cleanup. Imports retain a
tab-session rollback across reloads. The interface can clear both the tab queue
and accessible legacy queue keys; clearing browser site data also removes them.

## Explicit sharing

The normal address bar does not persist global search text, Research Structuring
Desk questions, Desk filters, or source-passage anchors. Ordinary Desk edits
remain in page memory. When a reader explicitly selects **Copy view**, the
copied URL may include the current question, filters, and exact source anchor so
that view can be shared. Treat that URL as public and use the feature only for
non-confidential research context. Exported workflow backups and copied
citations leave the browser only when the reader chooses where to send or save
them.

## Contact

For security-sensitive reports, follow [SECURITY.md](SECURITY.md). For other
questions, use the repository's public issue tracker without including private
or confidential information.
