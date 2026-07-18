import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).parent
PROMOTED_OUTPUTS = (
    'all_posts.json',
    'medium_posts.json',
    'all_sources_posts.json',
    'articles_index.json',
    'trades_extracted.json',
    'snapshot_manifest.json',
    '.direction_cache.json',
)


FAKE_PYTHON = r'''#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path


def write_json(path, value):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(value) + '\n', encoding='utf-8')


def option(name):
    index = sys.argv.index(name)
    return sys.argv[index + 1]


with open(os.environ['FAKE_PYTHON_LOG'], 'a', encoding='utf-8') as handle:
    handle.write(' '.join(sys.argv[1:]) + '\n')

arguments = sys.argv[1:]
if arguments[:3] == ['-m', 'unittest', '-q']:
    raise SystemExit(42 if os.environ.get('FAKE_FAILURE') == 'regression' else 0)
if not arguments:
    raise SystemExit(90)

script = Path(arguments[0]).name
if script == 'fetch_all_posts.py':
    write_json(os.environ['POSTS_OUTPUT'], [{'candidate': 'substack'}])
    write_json(os.environ['ARTICLES_OUTPUT'], [{'candidate': 'substack-article'}])
    write_json(os.environ['FETCH_STATUS_OUTPUT'], {'candidate': 'substack-status'})
elif script == 'fetch_medium_posts.py':
    write_json(os.environ['MEDIUM_OUTPUT'], [{'candidate': 'medium'}])
    write_json(os.environ['FETCH_STATUS_OUTPUT'], {'candidate': 'medium-status'})
elif script == 'merge_article_sources.py':
    write_json(os.environ['POSTS_OUTPUT'], [{'candidate': 'combined'}])
    write_json(os.environ['ARTICLES_OUTPUT'], [{'candidate': 'articles'}])
    write_json(os.environ['DEDUPE_REPORT_OUTPUT'], {'candidate': 'dedupe'})
elif script == 'extract_trades.py':
    write_json(os.environ['TRADES_OUTPUT'], [{'candidate': 'raw-trades'}])
elif script == 'filter_trades.py':
    write_json(os.environ['TRADES_OUTPUT'], [{'candidate': 'filtered-trades'}])
elif script == 'llm_direction.py':
    cache = Path(os.environ['DIRECTION_CACHE_PATH'])
    root_cache = Path(os.environ['FAKE_REPO_ROOT']) / '.direction_cache.json'
    if cache == root_cache or cache.parent == root_cache.parent:
        raise SystemExit(88)
    write_json(cache, {'candidate': 'direction-cache'})
    cache.with_name(cache.name + '.tmp').write_text('incomplete cache write', encoding='utf-8')
elif script == 'write_snapshot_manifest.py':
    write_json(option('--output'), {'candidate': 'manifest'})
elif script == 'validate_pipeline.py':
    raise SystemExit(41 if os.environ.get('FAKE_FAILURE') == 'validation' else 0)
else:
    raise SystemExit(91)
'''


FAKE_GIT = r'''#!/usr/bin/env bash
set -eu
printf '%s\n' "$*" >> "$FAKE_GIT_LOG"
if [ "${1:-}" = "branch" ] && [ "${2:-}" = "--show-current" ]; then
    printf 'main\n'
    exit 0
fi
if [ "${1:-}" = "status" ]; then
    exit 0
fi
if [ "${1:-}" = "pull" ]; then
    exit 0
fi
if [ "${1:-}" = "add" ]; then
    exit 0
fi
if [ "${1:-}" = "diff" ]; then
    exit 0
fi
if [ "${1:-}" = "push" ]; then
    exit 0
fi
printf 'unexpected fake git call: %s\n' "$*" >&2
exit 92
'''


FAKE_MV = r'''#!/usr/bin/env bash
set -eu
count=0
if [ -f "$FAKE_MV_STATE" ]; then
    count=$(sed -n '1p' "$FAKE_MV_STATE")
fi
count=$((count + 1))
printf '%s\n' "$count" > "$FAKE_MV_STATE"
if [ "${FAKE_MV_FAIL_AT:-0}" -eq "$count" ]; then
    printf 'injected mv failure at call %s\n' "$count" >&2
    exit 73
fi
exec /bin/mv "$@"
'''


class RefreshTransactionTests(unittest.TestCase):
    def setUp(self):
        self.case = tempfile.TemporaryDirectory(prefix='nrt-refresh-transaction-')
        self.base = Path(self.case.name)
        self.repo = self.base / 'repo'
        self.fake_bin = self.base / 'bin'
        self.home = self.base / 'home'
        self.tmp = self.base / 'tmp'
        for directory in (self.repo, self.fake_bin, self.home, self.tmp):
            directory.mkdir()

        shutil.copyfile(ROOT / 'refresh.sh', self.repo / 'refresh.sh')
        self.fake_python = self.fake_bin / 'python3'
        self.fake_git = self.fake_bin / 'git'
        self.fake_mv = self.fake_bin / 'mv'
        self._write_executable(
            self.fake_python,
            FAKE_PYTHON.replace('#!/usr/bin/env python3', f'#!{sys.executable}', 1),
        )
        self._write_executable(self.fake_git, FAKE_GIT)
        self._write_executable(self.fake_mv, FAKE_MV)

        self.before = {}
        for index, name in enumerate(PROMOTED_OUTPUTS):
            payload = f'old-{index}-{name}\n'.encode('utf-8')
            (self.repo / name).write_bytes(payload)
            self.before[name] = payload

        self.environment = os.environ.copy()
        self.environment.update({
            'PATH': str(self.fake_bin) + os.pathsep + self.environment.get('PATH', ''),
            'PYTHON_BIN': str(self.fake_python),
            'HOME': str(self.home),
            'TMPDIR': str(self.tmp),
            'FORCE_REFRESH': '1',
            'FAKE_REPO_ROOT': str(self.repo),
            'FAKE_PYTHON_LOG': str(self.base / 'python.log'),
            'FAKE_GIT_LOG': str(self.base / 'git.log'),
            'FAKE_MV_STATE': str(self.base / 'mv-state'),
            'FAKE_MV_FAIL_AT': '0',
        })

    def tearDown(self):
        self.case.cleanup()

    @staticmethod
    def _write_executable(path, value):
        path.write_text(textwrap.dedent(value), encoding='utf-8')
        path.chmod(0o755)

    def run_refresh(self, failure, mv_fail_at=0):
        environment = self.environment.copy()
        environment['FAKE_FAILURE'] = failure
        environment['FAKE_MV_FAIL_AT'] = str(mv_fail_at)
        return subprocess.run(
            ['/bin/bash', str(self.repo / 'refresh.sh')],
            cwd=self.repo,
            env=environment,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

    def assert_previous_state_is_exact_and_temporary_state_is_gone(self):
        for name, expected in self.before.items():
            self.assertEqual(
                (self.repo / name).read_bytes(),
                expected,
                f'{name} was not restored byte-for-byte',
            )
        self.assertEqual(list(self.repo.glob('*.tmp')), [])
        self.assertEqual(list(self.tmp.glob('substack-trades-refresh.*')), [])
        self.assertFalse((self.tmp / 'com.navnoor.substacktrades.lock').exists())

    def invocation_log(self):
        path = self.base / 'python.log'
        return path.read_text(encoding='utf-8') if path.exists() else ''

    def git_log(self):
        path = self.base / 'git.log'
        return path.read_text(encoding='utf-8') if path.exists() else ''

    def test_validation_failure_never_promotes_candidate_cache_or_snapshot(self):
        result = self.run_refresh('validation')
        self.assertEqual(result.returncode, 41, result.stdout + result.stderr)
        self.assertIn('validate_pipeline.py', self.invocation_log())
        self.assertNotIn('-m unittest -q', self.invocation_log())
        self.assertNotIn('\nadd ', '\n' + self.git_log())
        self.assert_previous_state_is_exact_and_temporary_state_is_gone()

    def test_regression_failure_restores_promoted_cache_and_snapshot(self):
        result = self.run_refresh('regression')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('-m unittest -q', self.invocation_log())
        self.assertIn('restoring the previous local snapshot', result.stderr)
        self.assertNotIn('\nadd ', '\n' + self.git_log())
        self.assert_previous_state_is_exact_and_temporary_state_is_gone()

    def test_mid_promotion_failure_rolls_back_every_file_and_cleans_candidates(self):
        result = self.run_refresh('promotion', mv_fail_at=4)
        self.assertEqual(result.returncode, 73, result.stdout + result.stderr)
        self.assertNotIn('-m unittest -q', self.invocation_log())
        self.assertIn('injected mv failure at call 4', result.stderr)
        self.assertIn('restoring the previous local snapshot', result.stderr)
        self.assertNotIn('\nadd ', '\n' + self.git_log())
        self.assert_previous_state_is_exact_and_temporary_state_is_gone()


if __name__ == '__main__':
    unittest.main()
