# Security policy

## Supported version

Only the current production release built from `main` is supported. The live
release exposes its exact Git revision. It is checked against that revision and
the tested artifact fingerprints after deployment, with an independent check
scheduled every four hours.

## Reporting a vulnerability

Do not publish exploit details, secrets, personal information, or confidential
research in a public issue. Use the repository's enabled GitHub private
vulnerability-reporting flow under **Security → Advisories → Report a
vulnerability**. Include the affected URL, impact, reproduction steps, and a
safe proof of concept.

Reports should receive an acknowledgement as soon as practical. A confirmed
critical issue should block or roll back a launch until a tested fix is live.

## Security model and boundaries

- The application is a static GitHub Pages site with no application backend,
  login, payment flow, or server-side secret.
- Release assets are generated in CI, action dependencies are pinned to full
  commit SHAs, deployment uses least-privilege permissions, and production is
  verified against exact build fingerprints.
- Inline scripts are restricted by exact Content Security Policy hashes. Inline
  styles remain allowed because the generated application currently uses a
  single-file style system.
- GitHub Pages controls response headers. Some defense-in-depth response headers
  available on a custom edge host cannot be set by this repository. The client
  refuses to run when embedded and attempts to escape a frame, but a dedicated
  host with a response-level `frame-ancestors 'none'` policy would be stronger.
- Decision packets are plaintext, tab-session workflow aids and backups are
  plaintext files—not a secure data vault or an enterprise audit record. The
  session ends with the top-level tab. Never store confidential or regulated
  information in either form.

Dependencies and GitHub Actions pins are reviewed by Dependabot. Security fixes
must pass the same regression, data-integrity, script-compilation, artifact, and
post-deploy smoke gates as other releases.
