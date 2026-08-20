import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parent
SUPERVISOR = ROOT / 'scheduled_refresh.sh'


class ScheduledRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.test_root = Path(self.temporary_directory.name)
        self.fake_bin = self.test_root / 'bin'
        self.fake_bin.mkdir()
        self.counter = self.test_root / 'attempts'
        self.sleep_log = self.test_root / 'sleep.log'
        shutil.copy2(SUPERVISOR, self.test_root / 'scheduled_refresh.sh')
        self.write_executable(
            self.test_root / 'refresh.sh',
            '#!/bin/bash\n'
            'set -eu\n'
            '[ "${REFRESH_BUSY_EXIT_CODE-}" = "75" ] || exit 99\n'
            'attempt=0\n'
            'if [ -f "$FAKE_ATTEMPT_COUNTER" ]; then\n'
            '    attempt=$(sed -n "1p" "$FAKE_ATTEMPT_COUNTER")\n'
            'fi\n'
            'attempt=$((attempt + 1))\n'
            'printf \'%s\\n\' "$attempt" > "$FAKE_ATTEMPT_COUNTER"\n'
            'IFS=, read -r -a results <<< "$FAKE_REFRESH_RESULTS"\n'
            'index=$((attempt - 1))\n'
            'exit "${results[$index]}"\n',
        )
        self.write_executable(
            self.fake_bin / 'sleep',
            '#!/bin/bash\n'
            'set -eu\n'
            'printf \'%s\\n\' "$1" >> "$FAKE_SLEEP_LOG"\n',
        )

    @staticmethod
    def write_executable(path, content):
        path.write_text(content, encoding='utf-8')
        path.chmod(0o755)

    def run_supervisor(self, results, **extra_environment):
        environment = os.environ.copy()
        environment.update({
            'FAKE_ATTEMPT_COUNTER': str(self.counter),
            'FAKE_REFRESH_RESULTS': results,
            'FAKE_SLEEP_LOG': str(self.sleep_log),
            'PATH': f'{self.fake_bin}:{environment.get("PATH", "")}',
        })
        environment.update(extra_environment)
        return subprocess.run(
            ['/bin/bash', str(self.test_root / 'scheduled_refresh.sh')],
            cwd=self.test_root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    def attempts(self):
        if not self.counter.exists():
            return 0
        return int(self.counter.read_text(encoding='utf-8').strip())

    def sleeps(self):
        if not self.sleep_log.exists():
            return []
        return self.sleep_log.read_text(encoding='utf-8').splitlines()

    def test_first_attempt_success_exits_without_a_retry(self):
        result = self.run_supervisor('0')

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.attempts(), 1)
        self.assertEqual(self.sleeps(), [])
        self.assertIn('Scheduled refresh attempt 1/3', result.stdout)
        self.assertNotIn('recovered', result.stdout)

    def test_failure_retries_after_the_default_delay_and_can_recover(self):
        result = self.run_supervisor('71,72,0')

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.attempts(), 3)
        self.assertEqual(self.sleeps(), ['900', '900'])
        self.assertIn('attempt 1/3 failed with exit code 71', result.stderr)
        self.assertIn('attempt 2/3 failed with exit code 72', result.stderr)
        self.assertIn('Scheduled refresh recovered on attempt 3/3', result.stdout)

    def test_live_lock_busy_signal_is_retried_instead_of_accepted_as_success(self):
        result = self.run_supervisor('75,0')

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.attempts(), 2)
        self.assertEqual(self.sleeps(), ['900'])
        self.assertIn('attempt 1/3 failed with exit code 75', result.stderr)
        self.assertIn('Scheduled refresh recovered on attempt 2/3', result.stdout)

    def test_three_failures_stop_and_preserve_the_final_exit_code(self):
        result = self.run_supervisor('71,72,73')

        self.assertEqual(result.returncode, 73, result.stdout + result.stderr)
        self.assertEqual(self.attempts(), 3)
        self.assertEqual(self.sleeps(), ['900', '900'])
        self.assertIn(
            'failed after 3 attempts; preserving final exit code 73',
            result.stderr,
        )
        self.assertNotIn('attempt 4/3', result.stdout + result.stderr)

    def test_invalid_delay_fails_before_starting_refresh(self):
        for invalid in ('-1', '1.5', '86401', 'invalid'):
            with self.subTest(invalid=invalid):
                self.counter.unlink(missing_ok=True)
                result = self.run_supervisor(
                    '0', SCHEDULED_REFRESH_RETRY_DELAY_SECONDS=invalid,
                )
                self.assertEqual(
                    result.returncode, 64, result.stdout + result.stderr,
                )
                self.assertEqual(self.attempts(), 0)
                self.assertIn('must be an integer from 0 to 86400', result.stderr)


if __name__ == '__main__':
    unittest.main()
