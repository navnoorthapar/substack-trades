"""Present the tracked snapshot to test fixtures as a freshly checked one.

The pipeline contracts fail closed on stale data: a snapshot whose recorded
check times are more than sixteen hours old is refused by the data contract,
the release validator, and the deployable-snapshot validator alike. That is
correct for publishing, but the tracked snapshot ages between scheduled
refreshes, so a fixture that feeds it back into those contracts starts failing
purely because time passed. Any push made long enough after the last refresh
would then fail continuous integration for a reason unrelated to the change.

Rebasing solves that without softening a single contract. Every recorded check
time is shifted by the same delta, so the manifest lands on the current clock
while the ordering and lag invariants the contracts enforce stay exactly as
recorded. Only the check times move; publication times are content-derived and
are deliberately left untouched.
"""

import copy
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

TRACKED_DATA_FILES = ('articles_index.json', 'trades_extracted.json')
SNAPSHOT_MANIFEST = 'snapshot_manifest.json'


def _parse(value):
    return datetime.fromisoformat(str(value).replace('Z', '+00:00'))


def _format(moment):
    return (
        moment.astimezone(timezone.utc)
        .isoformat(timespec='seconds')
        .replace('+00:00', 'Z')
    )


def rebase_snapshot_checks(manifest, now=None):
    """Return a copy of manifest whose newest check time lands on now."""
    rebased = copy.deepcopy(manifest)
    checked_at = rebased.get('checked_at')
    if not checked_at:
        return rebased
    now = now or datetime.now(timezone.utc)
    shift = now - _parse(checked_at)
    rebased['checked_at'] = _format(_parse(checked_at) + shift)
    sources = rebased.get('sources')
    if isinstance(sources, dict):
        for status in sources.values():
            if isinstance(status, dict) and status.get('checked_at'):
                status['checked_at'] = _format(_parse(status['checked_at']) + shift)
    return rebased


def write_rebased_manifest(source_path, destination, now=None):
    """Write the tracked manifest to destination with rebased check times."""
    manifest = json.loads(Path(source_path).read_text(encoding='utf-8'))
    destination = Path(destination)
    destination.write_text(
        json.dumps(rebase_snapshot_checks(manifest, now),
                   ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8',
    )
    return destination


def materialize_source_tree(root, destination, now=None):
    """Copy the tracked build inputs into destination with fresh check times."""
    root = Path(root)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    for source in root.glob('*.py'):
        shutil.copy2(source, destination / source.name)
    for name in TRACKED_DATA_FILES:
        shutil.copy2(root / name, destination / name)
    shutil.copytree(root / 'assets', destination / 'assets', dirs_exist_ok=True)
    write_rebased_manifest(root / SNAPSHOT_MANIFEST,
                           destination / SNAPSHOT_MANIFEST, now)
    return destination
