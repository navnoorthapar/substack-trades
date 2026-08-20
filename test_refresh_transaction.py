import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).parent
PROMOTED_OUTPUTS = (
    'all_posts.json',
    'medium_posts.json',
    'patreon_registry.json',
    'all_sources_posts.json',
    'articles_index.json',
    'trades_extracted.json',
    'snapshot_manifest.json',
    '.direction_cache.json',
    'treasury_curve.json',
)


FAKE_PYTHON = r'''#!/usr/bin/env python3
import json
import os
import sys
import time
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
    block_path = os.environ.get('FAKE_BLOCK_AT_REGRESSION_FILE')
    if block_path:
        Path(block_path).write_text('blocked\n', encoding='utf-8')
        time.sleep(60)
    raise SystemExit(42 if os.environ.get('FAKE_FAILURE') == 'regression' else 0)
if arguments[:1] == ['-c']:
    print('1')
    raise SystemExit(0)
if not arguments:
    raise SystemExit(90)

script = Path(arguments[0]).name
if script == 'fetch_all_posts.py':
    write_json(os.environ['POSTS_OUTPUT'], [{'candidate': 'substack'}])
    write_json(os.environ['ARTICLES_OUTPUT'], [{'candidate': 'substack-article'}])
    write_json(os.environ['FETCH_STATUS_OUTPUT'], {'candidate': 'substack-status'})
elif script == 'fetch_medium_posts.py':
    if len(arguments) != 1 or not Path(arguments[0]).is_absolute():
        raise SystemExit(88)
    for forbidden in ('MEDIUM_OUTPUT', 'PREVIOUS_MEDIUM', 'FETCH_STATUS_OUTPUT'):
        if forbidden in os.environ:
            raise SystemExit(89)
    write_json(Path.cwd() / 'medium.candidate.json', [{'candidate': 'medium'}])
    write_json(Path.cwd() / 'medium-status.json', {'candidate': 'medium-status'})
elif script == 'fetch_patreon_posts.py':
    write_json(os.environ['PATREON_OUTPUT'], [{'candidate': 'patreon'}])
    write_json(os.environ['PATREON_STATUS_OUTPUT'], {'candidate': 'patreon-status'})
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
elif script == 'fetch_treasury_curve.py':
    if os.environ.get('FAKE_FAILURE') == 'treasury':
        raise SystemExit(44)
    print(json.dumps({
        'schema_version': 1,
        'source': {'name': 'candidate'},
        'observations': {'2026-01-02': {'candidate': 'curve'}},
    }))
elif script == 'write_snapshot_manifest.py':
    write_json(option('--output'), {'candidate': 'manifest'})
elif script == 'source_health.py':
    print('Source health DEGRADED: fake transient fallback')
    raise SystemExit(0)
elif script == 'validate_pipeline.py':
    raise SystemExit(41 if os.environ.get('FAKE_FAILURE') == 'validation' else 0)
elif script == 'build_site.py':
    site = Path(os.environ['SITE_OUTPUT_DIR'])
    site.mkdir(parents=True)
    (site / 'index.html').write_text('candidate release', encoding='utf-8')
elif script == 'validate_release.py':
    raise SystemExit(43 if os.environ.get('FAKE_FAILURE') == 'release' else 0)
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
    if [ -n "${FAKE_BLOCK_AT_STATUS_FILE:-}" ]; then
        : > "$FAKE_BLOCK_AT_STATUS_FILE"
        sleep 60
    fi
    exit 0
fi
if [ "${1:-}" = "pull" ]; then
    exit 0
fi
if [ "${1:-}" = "add" ]; then
    if [ "${FAKE_FAILURE:-}" = "git-add" ]; then
        exit 70
    fi
    exit 0
fi
if [ "${1:-}" = "diff" ]; then
    if [ "${FAKE_FAILURE:-}" = "git-commit" ]; then
        exit 1
    fi
    exit 0
fi
if [ "${1:-}" = "commit" ]; then
    if [ "${FAKE_FAILURE:-}" = "git-commit" ]; then
        exit 71
    fi
    exit 0
fi
if [ "${1:-}" = "reset" ]; then
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
        # Keep this harness independent of a parent scheduled refresh. Tests
        # opt into the private scheduler contention code explicitly below.
        self.environment.pop('REFRESH_BUSY_EXIT_CODE', None)
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

    def run_refresh(self, failure, mv_fail_at=0, busy_exit_code=None):
        environment = self.environment.copy()
        environment['FAKE_FAILURE'] = failure
        environment['FAKE_MV_FAIL_AT'] = str(mv_fail_at)
        if busy_exit_code is not None:
            environment['REFRESH_BUSY_EXIT_CODE'] = busy_exit_code
        return subprocess.run(
            ['/bin/bash', str(self.repo / 'refresh.sh')],
            cwd=self.repo,
            env=environment,
            capture_output=True,
            text=True,
            # A fake-tool refresh runs in about three seconds, but this suite
            # also runs inside the pre-push release gate while the machine is
            # building the site. Keep the bound generous enough to survive that
            # contention and still fail fast on a genuine hang.
            timeout=60,
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

    def make_live_refresh_lock(self):
        lock = self.tmp / 'com.navnoor.substacktrades.lock'
        lock.mkdir()
        return lock

    @staticmethod
    def process_field(pid, field):
        result = subprocess.run(
            ['/bin/ps', '-ww', '-p', str(pid), '-o', f'{field}='],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        return result.stdout.strip()

    def write_lock_identity(self, lock, pid, command=None):
        (lock / 'pid').write_text(f'{pid}\n', encoding='utf-8')
        (lock / 'process-start').write_text(
            self.process_field(pid, 'lstart') + '\n', encoding='utf-8',
        )
        (lock / 'process-command').write_text(
            (command or self.process_field(pid, 'command')) + '\n',
            encoding='utf-8',
        )
        (lock / 'repository-root').write_text(
            str(self.repo.resolve()) + '\n', encoding='utf-8',
        )
        (lock / 'ready').write_text('ready\n', encoding='utf-8')

    @staticmethod
    def stop_process(process):
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        for stream in (process.stdout, process.stderr):
            if stream is not None and not stream.closed:
                stream.close()

    def invocation_log(self):
        path = self.base / 'python.log'
        return path.read_text(encoding='utf-8') if path.exists() else ''

    def git_log(self):
        path = self.base / 'git.log'
        return path.read_text(encoding='utf-8') if path.exists() else ''

    def assert_retry_publishes_from_a_clean_restored_state(self, failure, code):
        failed = self.run_refresh(failure)
        self.assertEqual(failed.returncode, code, failed.stdout + failed.stderr)
        self.assertIn(
            'restoring the previous local snapshot',
            failed.stderr,
        )
        self.assertIn('\nreset --quiet HEAD -- ', '\n' + self.git_log())
        self.assertNotIn('\npush ', '\n' + self.git_log())
        self.assert_previous_state_is_exact_and_temporary_state_is_gone()

        retry = self.run_refresh('')
        self.assertEqual(retry.returncode, 0, retry.stdout + retry.stderr)
        self.assertIn('\npush origin main', '\n' + self.git_log())
        self.assertTrue(
            any((self.repo / name).read_bytes() != self.before[name] for name in PROMOTED_OUTPUTS),
            'the successful retry did not retain its validated candidate',
        )
        self.assertEqual(list(self.tmp.glob('substack-trades-refresh.*')), [])
        self.assertFalse((self.tmp / 'com.navnoor.substacktrades.lock').exists())

    def test_live_lock_is_clean_for_manual_runs_but_retryable_for_scheduler(self):
        blocked = self.base / 'incumbent-blocked'
        incumbent_environment = self.environment.copy()
        incumbent_environment['FAKE_FAILURE'] = ''
        incumbent_environment['FAKE_BLOCK_AT_STATUS_FILE'] = str(blocked)
        incumbent = subprocess.Popen(
            ['/bin/bash', str(self.repo.resolve() / 'refresh.sh')],
            cwd=self.repo,
            env=incumbent_environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            lock = self.tmp / 'com.navnoor.substacktrades.lock'
            for _ in range(100):
                if blocked.exists() and (lock / 'ready').is_file():
                    break
                if incumbent.poll() is not None:
                    stdout, stderr = incumbent.communicate()
                    self.fail(f'incumbent exited early:\n{stdout}\n{stderr}')
                time.sleep(0.05)
            else:
                self.fail('incumbent refresh did not establish its live lock')

            manual = self.run_refresh('')
            self.assertEqual(manual.returncode, 0, manual.stdout + manual.stderr)
            self.assertIn('exiting cleanly', manual.stdout)
            self.assertTrue(lock.is_dir())

            scheduled = self.run_refresh('', busy_exit_code='75')
            self.assertEqual(
                scheduled.returncode, 75, scheduled.stdout + scheduled.stderr,
            )
            self.assertIn(
                'deferring this scheduled attempt for retry', scheduled.stderr,
            )
            self.assertTrue(lock.is_dir())
            self.assertNotIn('fetch_all_posts.py', self.invocation_log())
        finally:
            self.stop_process(incumbent)

    def test_live_lock_accepts_a_relative_refresh_script_path(self):
        blocked = self.base / 'relative-path-incumbent-blocked'
        incumbent_environment = self.environment.copy()
        incumbent_environment['FAKE_FAILURE'] = ''
        incumbent_environment['FAKE_BLOCK_AT_STATUS_FILE'] = str(blocked)
        incumbent = subprocess.Popen(
            ['/bin/bash', './refresh.sh'],
            cwd=self.repo,
            env=incumbent_environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            lock = self.tmp / 'com.navnoor.substacktrades.lock'
            for _ in range(100):
                if blocked.exists() and (lock / 'ready').is_file():
                    break
                if incumbent.poll() is not None:
                    stdout, stderr = incumbent.communicate()
                    self.fail(f'incumbent exited early:\n{stdout}\n{stderr}')
                time.sleep(0.05)
            else:
                self.fail(
                    'relative-path incumbent did not establish its live lock'
                )

            scheduled = self.run_refresh('', busy_exit_code='75')

            self.assertEqual(
                scheduled.returncode, 75,
                scheduled.stdout + scheduled.stderr,
            )
            self.assertIn(
                'deferring this scheduled attempt for retry',
                scheduled.stderr,
            )
            self.assertTrue(lock.is_dir())
            self.assertNotIn('\npush origin main', '\n' + self.git_log())
            self.assertNotIn('fetch_all_posts.py', self.invocation_log())
        finally:
            self.stop_process(incumbent)

    def test_live_lock_accepts_an_alternate_bash_executable_path(self):
        alternate_bin = self.base / 'alternate-bash-bin'
        alternate_bin.mkdir()
        alternate_bash = alternate_bin / 'bash'
        alternate_bash.symlink_to('/bin/bash')
        blocked = self.base / 'alternate-bash-incumbent-blocked'
        incumbent_environment = self.environment.copy()
        incumbent_environment['FAKE_FAILURE'] = ''
        incumbent_environment['FAKE_BLOCK_AT_STATUS_FILE'] = str(blocked)
        incumbent = subprocess.Popen(
            [str(alternate_bash), str(self.repo.resolve() / 'refresh.sh')],
            cwd=self.repo,
            env=incumbent_environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            lock = self.tmp / 'com.navnoor.substacktrades.lock'
            for _ in range(100):
                if blocked.exists() and (lock / 'ready').is_file():
                    break
                if incumbent.poll() is not None:
                    stdout, stderr = incumbent.communicate()
                    self.fail(f'incumbent exited early:\n{stdout}\n{stderr}')
                time.sleep(0.05)
            else:
                self.fail(
                    'alternate-Bash incumbent did not establish its live lock'
                )

            scheduled = self.run_refresh('', busy_exit_code='75')

            self.assertEqual(
                scheduled.returncode, 75,
                scheduled.stdout + scheduled.stderr,
            )
            self.assertIn(
                'deferring this scheduled attempt for retry',
                scheduled.stderr,
            )
            self.assertTrue(lock.is_dir())
            self.assertNotIn('\npush origin main', '\n' + self.git_log())
            self.assertNotIn('fetch_all_posts.py', self.invocation_log())
        finally:
            self.stop_process(incumbent)

    def test_unrelated_live_pid_cannot_hold_the_refresh_lock(self):
        unrelated = subprocess.Popen(
            ['/bin/sleep', '60'],
            cwd=self.repo,
            start_new_session=True,
        )
        try:
            lock = self.make_live_refresh_lock()
            self.write_lock_identity(lock, unrelated.pid)

            result = self.run_refresh('')

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn('already running', result.stdout + result.stderr)
            self.assertIn('\npush origin main', '\n' + self.git_log())
            self.assertFalse(lock.exists())
        finally:
            self.stop_process(unrelated)

    def test_reused_refresh_like_pid_with_wrong_start_token_is_stale(self):
        reused = subprocess.Popen(
            ['/bin/bash', '-c', 'sleep 60', 'refresh.sh'],
            cwd=self.repo,
            start_new_session=True,
        )
        try:
            lock = self.make_live_refresh_lock()
            self.write_lock_identity(lock, reused.pid)
            (lock / 'process-start').write_text(
                'Mon Jan  1 00:00:00 1900\n', encoding='utf-8',
            )

            result = self.run_refresh('')

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn('already running', result.stdout + result.stderr)
            self.assertIn('\npush origin main', '\n' + self.git_log())
            self.assertFalse(lock.exists())
        finally:
            self.stop_process(reused)

    def test_busy_exit_override_rejects_values_outside_scheduler_contract(self):
        for invalid in ('', '-1', '1', '74', '76', 'invalid'):
            with self.subTest(invalid=invalid):
                result = self.run_refresh('', busy_exit_code=invalid)
                self.assertEqual(
                    result.returncode, 64, result.stdout + result.stderr,
                )
                self.assertIn(
                    'REFRESH_BUSY_EXIT_CODE must be unset, 0, or 75',
                    result.stderr,
                )

    def test_a_treasury_outage_keeps_the_tracked_curve_and_still_publishes(self):
        """A published rate series is not this pipeline's to produce.

        The curve is fetched from Treasury, so a feed outage must not stop a
        research refresh; the run keeps the curve that already shipped and
        publishes the rest of the snapshot.
        """
        result = self.run_refresh('treasury')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('keeping the tracked curve', result.stderr)
        self.assertEqual(
            (self.repo / 'treasury_curve.json').read_bytes(),
            self.before['treasury_curve.json'],
            'a feed outage must leave the tracked curve exactly as it was',
        )
        self.assertIn('\npush origin main', '\n' + self.git_log())
        self.assertTrue(
            any(
                (self.repo / name).read_bytes() != self.before[name]
                for name in PROMOTED_OUTPUTS if name != 'treasury_curve.json'
            ),
            'the refresh should still have promoted its research snapshot',
        )

    def test_treasury_fetch_has_no_network_or_path_arguments(self):
        result = self.run_refresh('')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.invocation_log().splitlines()
        treasury_calls = [
            call for call in calls if call.startswith('fetch_treasury_curve.py')
        ]
        self.assertEqual(treasury_calls, ['fetch_treasury_curve.py'])
        self.assertEqual(
            json.loads((self.repo / 'treasury_curve.json').read_text(encoding='utf-8')),
            {
                'schema_version': 1,
                'source': {'name': 'candidate'},
                'observations': {'2026-01-02': {'candidate': 'curve'}},
            },
        )

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

    def assert_signal_during_regression_restores_snapshot(
        self, signal_number, expected_exit,
    ):
        signal_name = signal.Signals(signal_number).name
        blocked = self.base / f'regression-blocked-{signal_name.lower()}'
        environment = self.environment.copy()
        environment.update({
            'FAKE_FAILURE': '',
            'FAKE_BLOCK_AT_REGRESSION_FILE': str(blocked),
        })
        process = subprocess.Popen(
            ['/bin/bash', str(self.repo / 'refresh.sh')],
            cwd=self.repo,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            for _ in range(200):
                if blocked.exists():
                    break
                if process.poll() is not None:
                    stdout, stderr = process.communicate()
                    self.fail(f'refresh exited before regression block:\n{stdout}\n{stderr}')
                time.sleep(0.05)
            else:
                self.fail('refresh did not reach the blocked regression stage')

            self.assertTrue(
                all(
                    (self.repo / name).read_bytes() != self.before[name]
                    for name in PROMOTED_OUTPUTS
                ),
                'the signal fixture did not reach the fully promoted boundary',
            )
            os.killpg(process.pid, signal_number)
            stdout, stderr = process.communicate(timeout=10)

            self.assertEqual(process.returncode, expected_exit, stdout + stderr)
            self.assertIn('restoring the previous local snapshot', stderr)
            self.assertIn(
                f'interrupted by {signal_name} (exit {expected_exit})', stderr,
            )
            self.assertNotIn('\nadd ', '\n' + self.git_log())
            self.assert_previous_state_is_exact_and_temporary_state_is_gone()
        finally:
            self.stop_process(process)

    def test_interrupt_during_regression_restores_promoted_snapshot(self):
        self.assert_signal_during_regression_restores_snapshot(signal.SIGINT, 130)

    def test_termination_during_regression_restores_promoted_snapshot(self):
        self.assert_signal_during_regression_restores_snapshot(signal.SIGTERM, 143)

    def test_release_artifact_failure_restores_snapshot_before_staging(self):
        result = self.run_refresh('release')
        self.assertEqual(result.returncode, 43, result.stdout + result.stderr)
        self.assertIn('build_site.py', self.invocation_log())
        self.assertIn('validate_release.py', self.invocation_log())
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

    def test_git_add_failure_restores_clean_state_and_next_run_retries(self):
        self.assert_retry_publishes_from_a_clean_restored_state('git-add', 70)

    def test_git_commit_failure_restores_clean_state_and_next_run_retries(self):
        self.assert_retry_publishes_from_a_clean_restored_state('git-commit', 71)


if __name__ == '__main__':
    unittest.main()
