# Navnoor Research Terminal

This project collects research from `navnoorbawa.substack.com` and
`medium.com/@navnoorbawa`, extracts structured investment observations, and
publishes the institutional research terminal at
<https://navnoorthapar.github.io/substack-trades/>.

## Architecture

Substack rejects cloud-datacenter traffic, so publication ingestion stays on a
logged-in Mac with a residential connection. Website builds and production
deployment run independently on GitHub Actions:

```text
Scheduled Mac
  Substack API ----> all_posts.json ---------\
                                             +-> cross-source dedupe
  Medium archive -> medium_posts.json -------/          |
                                                        +-> articles_index.json
                                                        +-> trades_extracted.json
                                                        +-> strict validation + tests
                                                        +-> commit tracked data
                                                                  |
                                                                  v
GitHub Actions
  tracked snapshot -> validation + tests -> _site/index.html
                                         -> immutable Pages artifact
                                         -> atomic production deployment
```

Medium's public author archive is paginated; its ten-item RSS feed is used only
as a fallback. Cross-posts are matched using Medium's explicit Substack notice,
normalized titles, subtitles plus dates, conservative similarity, and reviewed
mappings in `medium_dedupe_overrides.json`. Substack remains the canonical card
for cross-posts; only Medium-only articles are added.

`all_posts.json` and `all_sources_posts.json` stay local. The tracked pipeline
state is `medium_posts.json`, `articles_index.json`, `trades_extracted.json`, and
`.direction_cache.json`; retaining the Medium catalogue prevents a temporary
archive failure from erasing older articles. Production builds consume the
validated article and idea snapshots. Generated HTML is intentionally ignored:
every production artifact is rebuilt, tested, and deployed without a bot commit
or a second source of truth.

The core pipeline needs Python 3.9+, Git with authenticated write access to
`origin`, and network access to Substack and GitHub. It has no pip dependencies.
Ollama with `qwen2.5:14b` is optional; without it, refreshes preserve cached
classifications and keep the regex-only direction for new residuals.

## Install the scheduled updater

```bash
./install_automation.sh
```

The installer copies the versioned LaunchAgent into
`~/Library/LaunchAgents`, loads it, verifies it, and starts one refresh. It then
runs at 09:00, 13:00, and 22:00 local time and once after login.

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

Every push to `main` runs the regression suite, validates the tracked snapshot,
builds a fresh immutable artifact, and deploys it. Pull requests run the same
quality gate without production credentials or deployment. Production runs are
serialized and never cancelled midway; stale pull-request runs are cancelled.

Manually redeploy the current `main` snapshot without fetching publications:

```bash
gh workflow run update.yml --ref main
gh run list --workflow update.yml --limit 5
```

Only the scheduled Mac can discover new publication posts. A manual workflow
run rebuilds and redeploys the already tracked snapshot.

## Validate and preview locally

Run all regression tests and the strict local-data validation:

```bash
python3 -m unittest discover -s . -p 'test_*.py' -v
python3 validate_pipeline.py \
  --posts all_sources_posts.json \
  --articles articles_index.json \
  --trades trades_extracted.json
```

Build the ignored local preview and serve it at <http://localhost:8000>:

```bash
python3 build_site.py
python3 -m http.server 8000 --directory docs
```

If `automation_status.sh` reports `NOT LOADED`, enable the macOS background
item and rerun `./install_automation.sh`. If refresh reaches Git but cannot push,
verify credentials with `gh auth status`; any local commit still ahead of
`origin/main` is retried by the next successful refresh.
