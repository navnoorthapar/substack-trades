# Subscriber conversion playbook

Navnoor Research Terminal promotes paid research as a source-completeness
workflow, not as an advertisement and not as a simulated client-side paywall.
The public terminal proves what it has captured, identifies what remains at the
publication, and lets an interested reader continue to the exact source.

## Product contract

1. **Prove value before asking.** Search, filters, source passages, citations,
   related research, and the local decision workflow remain available without
   an account or interruption.
2. **Promote only demonstrated intent.** A complete conversion panel appears
   after a reader selects a canonical member-access Substack record. If the
   anonymous source supplied preview text, the panel labels that exact preview;
   otherwise it says that the release is metadata-only. A compact archive
   module may appear when other subscriber notes match the selected research
   context. There is no entry modal, countdown, repeated nag, or obscured text.
3. **Use article evidence, not invented promises.** Promotion surfaces use only
   release-derived facts: the article's research framing, access state, preview
   availability, and deterministic related-topic matches. The surrounding
   dossier may report captured research-role and observation coverage. Neither
   surface claims returns, alpha, holdings, conviction, recommendations, reader
   counts, scarcity, or professional endorsement.
4. **Keep access and capture separate.** `publication_access` states whether the
   canonical source is public, member-only, or unknown. `content_status` states
   whether this release contains a full capture, excerpt, or metadata registry.
   Neither field implies source quality or investment suitability.
5. **Preserve source identity.** A Substack subscriber preview links to its exact
   Substack article and separately to the publication's subscription page. A
   locked Medium article links to Medium. Patreon remains a metadata-only
   registry. An alternate source never inherits the canonical source's access
   claim.
6. **Publish only anonymously available member-source bytes.** GitHub Pages,
   the repository, social stubs, deferred assets, and all six `/data/`
   endpoints are public. A member-source record may therefore contain only an
   exact, hash-bound anonymous preview of at most 1,200 characters, or a
   metadata-only proof with no body text. It may never contain an authenticated,
   cached, or otherwise private body. Access to the complete article is
   enforced by the source publication, not hidden with browser code.

## Public preview boundary

`member_preview` is a publication proof, not a teaser invented by the terminal.
Every member-access Substack or Medium row carries exactly five fields:
`schema_version`, `surface`, `text`, `character_count`, and `body_sha256`. The
count must equal the exact text length, the digest must equal that exact text,
and the article brief and any derived observation must bind to the same digest.

- A non-empty paid Substack proof comes only from the anonymous Substack list
  surface and is capped at 1,200 characters including any terminal ellipsis.
- If no trustworthy anonymous preview exists, the proof is `metadata-only`, its
  text and count are empty/zero, and its brief contains no derived body spans.
- A legacy locked Medium cache without the same proof is scrubbed to
  metadata-only. A locked Medium subtitle is retained only when it occurs
  exactly inside the proven anonymous preview.
- Source collectors reject unrecognized audience/visibility enumeration values
  rather than guessing. A validated surface that exposes no access flag, such as
  a Medium RSS fallback, stays `unknown` and receives no member-access claim.
- `content_status: excerpt` is the access-safe container state. It does not by
  itself mean that body text exists; `member_preview.character_count` is the
  authoritative distinction between an indexed anonymous preview and metadata
  only.

At the 2026-08-08 migration checkpoint, the 493-row candidate snapshot contains
330 member-source records: 223 paid Substack rows and 107 locked Medium rows.
Sixty-four Substack rows have non-empty proven previews (14,500 characters in
total; 642 maximum on any row); the other 266 member-source rows are
metadata-only. The 101-row observation snapshot contains five observations
derived from, and digest-bound to, those exact anonymous previews. These counts
describe that checkpoint only and will change with publication refreshes.

## Reader journey

```text
Find relevant evidence
  -> inspect the public source passage and provenance when captured
  -> otherwise verify the explicit metadata-only boundary
  -> see a restrained member-source badge
  -> review article-specific coverage proof
  -> open the exact complete note or inspect current subscription options
  -> review price, trial, renewal, and cancellation terms on Substack
```

The primary action answers the reader's immediate question: **Read full note on
Substack**. The secondary action answers the broader archive question:
**See subscription plans**. Subscription prices and trial claims are never
hard-coded into the terminal because they can change; the current terms remain
adjacent to checkout on Substack.

## Institutional value hierarchy

- **Owner / CIO:** quickly establish why a note belongs in human diligence,
  which sources and research roles are documented, and where the complete
  context lives.
- **Portfolio manager:** connect thesis, catalyst, falsifier, risk, and
  implementation passages without mistaking extraction coverage for
  confidence.
- **Trader:** scan markets, underlyings, publication dates, and source context
  before leaving for the full note; no live-price or execution claim is made.
- **Quantitative researcher:** use deterministic IDs, source provenance,
  bounded passages, revision status, and public versioned exports.

The subscription promise should stay anchored to durable research utility: the
full research archive and strategy teardowns; deep dives on volatility,
calibration, and systematic design; and the ability to comment and ask questions
on posts. Founding access currently adds priority on research topics. These are
the benefits listed on the public subscription page at the 2026-08-08 review;
verify them before reusing this copy because the source offer can change. The
promise must not be framed as privileged signals or a guaranteed investment
outcome.

## Editorial workflow

For every paid Substack note:

1. Publish a meaningful free preview that contains one complete insight rather
   than a vague teaser.
2. Stop at a genuine unresolved research decision: methodology detail,
   falsifier, implementation constraint, evidence extension, or conclusion.
3. Make the paid inclusion concrete in the source article; do not imply an
   unseen section unless it exists.
4. Retain exact source title, subtitle, audience, revision provenance, and the
   bounded preview in the next terminal refresh.
5. Let the release derive its coverage proof and related premium notes from the
   validated snapshot. Do not hand-author conversion counts in HTML.
6. Keep at least one exemplary full public article available so a new reader can
   assess quality before purchasing.

Off-site publication work requires an owner or publication-admin action; this
repository cannot update Substack settings or emails. Before pasting any copy,
open every linked article in a signed-out browser, verify the public benefit list
and material checkout terms, and replace any starting article whose access has
changed. Offers or Substack Boost should be tested only after the core promise
and onboarding are stable. Change one variable per test: preview cutoff, benefit
copy, action label, or offer—not all at once.

## Ready-to-paste Substack copy

The templates below intentionally omit prices, discounts, subscriber counts,
trial availability, renewal cadence, and cancellation claims. Substack must show
the current material terms next to the purchase decision. The public starting
articles were full, signed-out-readable Substack posts at the 2026-08-08
checkpoint; perform the signed-out preflight above before publishing.

### About page

```text
Navnoor Research

How hedge funds actually make money, worked out from filings and publicly attributable evidence. Every number sourced.

The research focuses on strategy mechanics: what drives the trade, which assumptions matter, what could falsify the argument, and where implementation or market structure can break it when the evidence supports those questions. The aim is useful diligence, not a signal feed or a promise of returns.

Free readers receive occasional public research posts.

Paid readers receive:
• the full research archive and strategy teardowns;
• deep dives on volatility, calibration, and systematic design; and
• the ability to comment and ask questions on every post.

Founding members receive everything in paid access, plus priority on research topics.

Start with the public research:
• Optiver and European market structure: https://www.navnoorbawaresearch.com/p/optiver-wrote-the-neutral-fix-for
• Black-Scholes delta and the Hull-White correction: https://www.navnoorbawaresearch.com/p/black-scholes-delta-is-wrong-hull
• Mean-reversion speed bias at D. E. Shaw, Citadel, and Renaissance: https://www.navnoorbawaresearch.com/p/de-shaw-citadel-and-renaissance-run

Browse the evidence-organized public catalogue: https://navnoorthapar.github.io/substack-trades/

Review the current plans and purchase terms: https://www.navnoorbawaresearch.com/subscribe

Research is for information and education. It is not investment advice, a recommendation, or a representation of current holdings or performance.
```

### Free-reader welcome email

```text
Subject: Start here with Navnoor Research

Thanks for joining Navnoor Research.

I study how hedge funds actually make money using filings and publicly attributable evidence. Every number is sourced, and the work examines mechanisms, assumptions, risks, and falsifiers when the evidence supports them—not trade alerts or guaranteed outcomes.

Free subscribers receive occasional public research posts. Three useful starting points are:

1. Optiver and European market structure
https://www.navnoorbawaresearch.com/p/optiver-wrote-the-neutral-fix-for

2. Black-Scholes delta and the Hull-White correction
https://www.navnoorbawaresearch.com/p/black-scholes-delta-is-wrong-hull

3. Mean-reversion speed bias at D. E. Shaw, Citadel, and Renaissance
https://www.navnoorbawaresearch.com/p/de-shaw-citadel-and-renaissance-run

You can also search the public research catalogue by firm, market, underlying, and thesis:
https://navnoorthapar.github.io/substack-trades/

Paid access includes the full research archive and strategy teardowns, deep dives on volatility, calibration, and systematic design, and the ability to comment and ask questions on every post. If that would improve your research workflow, review the current plans and terms here:
https://www.navnoorbawaresearch.com/subscribe

Navnoor
```

### Paid-reader welcome email

```text
Subject: Your Navnoor Research access — start here

Thank you for supporting Navnoor Research.

Your paid access includes the full research archive and strategy teardowns, deep dives on volatility, calibration, and systematic design, and the ability to comment and ask questions on every post.

Start in three steps:

1. Open the full archive and choose the market, model, or manager closest to your current work:
https://www.navnoorbawaresearch.com/archive

2. Use the public research terminal to scan the catalogue, inspect source coverage, and find related work. Open the original Substack note while signed in to read anything reserved for subscribers:
https://navnoorthapar.github.io/substack-trades/

3. Comment on a post with the assumption, data point, or implementation risk you want examined. The subscription includes the ability to ask questions on every post.

Founding members can also suggest research topics for priority consideration.

This work is research and education, not investment advice or a promise of returns. Source coverage can organize diligence; it cannot replace verification or portfolio-specific judgment.

Navnoor
```

## Measurement and privacy

The terminal intentionally sends no search, filter, article-reading, selection,
or decision-queue event to the author or a third party. Outbound article and
subscription URLs contain no referral or workflow parameters. If the owner
evaluates conversion, use Substack's own publication-level subscriber dashboard
and retention measures; never join billing identity to terminal behavior.

Review retained value rather than clicks:

- free-to-paid conversion and, when a trial is currently offered, trial-to-paid
  conversion;
- annual share and 30/90-day retention;
- paid churn and gift/referral conversion;
- which editorial topic cohorts retain readers over time.

These are owner-side operating metrics and must never enter this public
repository or its generated data bundle.

## Release gates

These gates govern the candidate and future artifacts; they do not erase older
Git objects or retained release bundles. `LAUNCH-058` remains a P0 `BLOCKER`
until the owner explicitly decides whether the historical member-source material
is authorized or approves a coordinated destructive purge. Even an authorized
purge cannot guarantee recall of prior clones or third-party caches.

- Each rendered canonical paid Substack conversion panel has one safe
  exact-article action and one generic subscription-options action.
- Public or unknown sources never receive a member-access claim.
- Locked Medium and paid Patreon records never imply that Substack unlocks the
  original article.
- Paid Substack and locked Medium records cannot validate as `full` public
  captures.
- Copy states that captured roles and observations measure extraction coverage,
  not quality, recommendation, or suitability.
- External links are HTTPS, open only after a reader action, disclose the new
  tab, and use `noopener noreferrer`.
- Subscription URLs contain no query string, terminal state, referral tag, or
  local-workflow value.
- Keyboard focus, text contrast, 200% zoom, and narrow-screen layout remain
  usable; conversion controls never cover source evidence.
- No third-party request occurs on load, search, filter, read, or queue use.
- Generated crawler stubs and public data expose no authenticated or cached
  member-only body beyond the validated anonymous preview boundary.

## Research basis

The operating model adapts established patterns without copying their tracking
systems or overstating self-reported results:

- Navnoor Research's public subscription page is the source of truth for the
  free, paid, and founding benefit language used in the templates; live prices,
  offers, trial eligibility, renewal, and cancellation terms remain there:
  <https://www.navnoorbawaresearch.com/subscribe>
- Financial Times Strategies describes registration sampling and contextual
  offers after demonstrated engagement:
  <https://www.ftstrategies.com/en-gb/insights/how-the-financial-times-registration-strategy-transformed-anonymous-reach-into-recurring-revenue>
- Reuters Institute research reports that paying news readers value distinctive,
  high-quality, curated, exclusive, relevant coverage and a better experience:
  <https://reutersinstitute.politics.ox.ac.uk/paying-news-price-conscious-consumers-look-value-amid-cost-living-crisis>
- The Economist's subscription flow makes inclusions and material plan terms
  scannable before purchase: <https://www.economist.com/subscribe>
- Bloomberg Professional describes the product through decision utility,
  coverage capability, and workflow rather than article volume alone:
  <https://professional.bloomberg.com/products/bloomberg-terminal/news/introduction/>
- Substack documents free previews, trials, and welcome-email upgrade paths:
  <https://support.substack.com/hc/en-us/articles/4407989020308-How-do-I-publish-a-free-preview-of-a-paid-post-on-Substack>
  and
  <https://support.substack.com/hc/en-us/articles/24034796625428-How-do-I-set-up-welcome-emails-on-Substack>
- The FTC's subscription guidance is the minimum behavioral standard: disclose
  material terms, obtain informed consent, and avoid obstructive cancellation or
  dark patterns:
  <https://www.ftc.gov/news-events/news/press-releases/2021/10/ftc-ramp-enforcement-against-illegal-dark-patterns-trick-or-trap-consumers-subscriptions>

Publisher case-study improvements are treated as directional evidence, not as a
guarantee that the same conversion effect will occur here.
