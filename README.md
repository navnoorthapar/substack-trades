# Substack Trade Intelligence

This project fetches posts from `navnoorbawa.substack.com` and
`medium.com/@navnoorbawa`, extracts trade records, and publishes the dashboard at
<https://navnoorthapar.github.io/substack-trades/>.

## Architecture

Substack rejects GitHub-hosted datacenter traffic, so fetching and publishing
run from a logged-in Mac with a residential connection. Medium's public author
archive is paginated; the ten-item RSS feed is used only as a fallback. The
local pipeline is:

```text
Substack API ----> all_posts.json ---------\
                                           +-> cross-source dedupe -> all_sources_posts.json
Medium archive -> medium_posts.json -------/                         |
                                                                     +-> articles_index.json
                                                                     +-> trades_extracted.json
                                                                     +-> validation -> docs/index.html
                                                                                     -> GitHub Pages
```

`all_posts.json` and `all_sources_posts.json` stay local. `medium_posts.json` is
tracked as the last known complete Medium catalogue so a temporary API failure
cannot erase older articles. Cross-posts are matched by Medium's explicit
Substack notice, normalized/full titles, subtitles plus dates, conservative
similarity, and reviewed mappings in `medium_dedupe_overrides.json`. Substack is
kept as the canonical card; only Medium-only articles are added.

`articles_index.json`, `medium_posts.json`, `trades_extracted.json`,
`.direction_cache.json`, and `docs/index.html` are tracked. The GitHub Action
never fetches either publication; it only rebuilds `docs/index.html` after
relevant pushes or a manual dispatch.

The core pipeline needs Python 3.9+, Git with authenticated write access to
`origin`, and network access to Substack and GitHub. It has no pip dependencies.
Ollama with `qwen2.5:14b` is optional; without it, refreshes keep the regex-only
directions.

## Install the scheduled updater

```bash
./install_automation.sh
```

The installer copies the versioned LaunchAgent into
`~/Library/LaunchAgents`, loads it, verifies it, and starts one refresh. It then
runs at 09:00, 13:00, and 20:00 local time and once after login.

macOS may block the new background process. Open **System Settings -> General ->
Login Items & Extensions -> Allow in Background**, enable the `bash`/Unknown
Developer item associated with `com.navnoor.substacktrades`, then rerun
`./install_automation.sh`. The Mac must be logged in for this user LaunchAgent to
run.

## Refresh and status commands

Run a refresh immediately:

```bash
./refresh.sh
```

Bypass only the 30-minute duplicate-run guard:

```bash
FORCE_REFRESH=1 ./refresh.sh
```

Check whether automation is loaded and whether the last successful publish is
fresh:

```bash
./automation_status.sh
launchctl print "gui/$(id -u)/com.navnoor.substacktrades"
```

Inspect scheduled-run logs:

```bash
tail -n 100 "$HOME/Library/Logs/SubstackTrades/refresh.log"
tail -n 100 "$HOME/Library/Logs/SubstackTrades/refresh-error.log"
```

Trigger the build-only GitHub fallback after tracked data or builder changes:

```bash
gh workflow run update.yml
gh run list --workflow update.yml --limit 5
```

This fallback can regenerate the site, but it cannot discover new publication
posts; only the Mac refresh runs the live multi-source ingestion pipeline.

Validate the currently generated data without publishing it:

```bash
python3 validate_pipeline.py \
  --posts all_sources_posts.json \
  --articles articles_index.json \
  --trades trades_extracted.json
```

If `automation_status.sh` reports `NOT LOADED`, enable the macOS background
item and rerun the installer. If refresh reaches Git but cannot publish, verify
credentials with `gh auth status`; the next successful refresh always retries
any local commit that is still ahead of `origin/main`.
