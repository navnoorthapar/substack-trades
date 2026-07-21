# Privacy

Last updated: 2026-07-21

Navnoor Research Terminal is a static, public research-intake website. It is
designed to work without an account and without collecting reader data for the
project owner.

## What the site does not collect

The published application includes no analytics SDK, advertising pixel,
tracking cookie, session replay, account system, form submission, or background
telemetry. Search text, filters, reading activity, and decision-workflow entries
are not transmitted to this project.

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
cards and crawler stubs contain only bounded public title, source, publication
date, and route metadata. All of these files are public and may be cached by
browsers, search engines, GitHub Pages, and downstream consumers.

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

The normal address bar does not persist search text. When a reader explicitly
selects **Copy view**, the copied URL may include the current query and filters
so that view can be shared. Exported workflow backups and copied citations leave
the browser only when the reader chooses where to send or save them.

## Contact

For security-sensitive reports, follow [SECURITY.md](SECURITY.md). For other
questions, use the repository's public issue tracker without including private
or confidential information.
