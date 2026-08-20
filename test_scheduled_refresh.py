import os
import signal
import shutil
import subprocess
import tempfile
import time
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
            '[ -z "${SCHEDULED_REFRESH_RETRY_DELAY_SECONDS-}" ] || exit 98\n'
            'attempt=0\n'
            'if [ -f "$FAKE_ATTEMPT_COUNTER" ]; then\n'
            '    attempt=$(sed -n "1p" "$FAKE_ATTEMPT_COUNTER")\n'
            'fi\n'
            'attempt=$((attempt + 1))\n'
            'printf \'%s\\n\' "$attempt" > "$FAKE_ATTEMPT_COUNTER"\n'
            'if [ -n "${FAKE_REFRESH_BLOCK_FILE:-}" ]; then\n'
            '    trap \'printf "TERM\\n" > "$FAKE_REFRESH_TERM_LOG"; '
            'wait "$descendant" 2>/dev/null || true; exit 143\' TERM\n'
            '    /bin/sleep 60 &\n'
            '    descendant=$!\n'
            '    printf \'%s\\n\' "$descendant" > "$FAKE_REFRESH_DESCENDANT_PID_FILE"\n'
            '    : > "$FAKE_REFRESH_BLOCK_FILE"\n'
            '    wait "$descendant"\n'
            '    exit 97\n'
            'fi\n'
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
        environment = self.supervisor_environment(results, **extra_environment)
        return subprocess.run(
            ['/bin/bash', str(self.test_root / 'scheduled_refresh.sh')],
            cwd=self.test_root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    def supervisor_environment(self, results, **extra_environment):
        environment = os.environ.copy()
        # A parent scheduler may itself be running with a custom retry delay.
        # Every fixture starts from the public default and opts in explicitly.
        environment.pop('SCHEDULED_REFRESH_RETRY_DELAY_SECONDS', None)
        environment.update({
            'FAKE_ATTEMPT_COUNTER': str(self.counter),
            'FAKE_REFRESH_RESULTS': results,
            'FAKE_SLEEP_LOG': str(self.sleep_log),
            'PATH': f'{self.fake_bin}:{environment.get("PATH", "")}',
        })
        environment.update(extra_environment)
        return environment

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

    def test_custom_delay_is_used_only_by_the_supervisor(self):
        result = self.run_supervisor(
            '71,0', SCHEDULED_REFRESH_RETRY_DELAY_SECONDS='0',
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.attempts(), 2)
        self.assertEqual(self.sleeps(), ['0'])
        self.assertIn('Scheduled refresh recovered on attempt 2/3', result.stdout)

    def assert_parent_signal_is_forwarded(self, signal_number, expected_exit):
        signal_name = signal.Signals(signal_number).name
        blocked = self.test_root / f'blocked-{signal_name.lower()}'
        term_log = self.test_root / f'term-{signal_name.lower()}.log'
        descendant_log = self.test_root / f'descendant-{signal_name.lower()}.pid'
        environment = self.supervisor_environment(
            '0',
            FAKE_REFRESH_BLOCK_FILE=str(blocked),
            FAKE_REFRESH_TERM_LOG=str(term_log),
            FAKE_REFRESH_DESCENDANT_PID_FILE=str(descendant_log),
        )
        process = subprocess.Popen(
            ['/bin/bash', str(self.test_root / 'scheduled_refresh.sh')],
            cwd=self.test_root,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            for _ in range(100):
                if blocked.exists():
                    break
                if process.poll() is not None:
                    stdout, stderr = process.communicate()
                    self.fail(f'supervisor exited before child block:\n{stdout}\n{stderr}')
                time.sleep(0.05)
            else:
                self.fail('supervisor child did not enter the blocking fixture')
            descendant_pid = int(descendant_log.read_text(encoding='utf-8'))

            # Model launchd stopping its owned supervisor PID, not an
            # interactive terminal broadcasting to the whole process group.
            os.kill(process.pid, signal_number)
            stdout, stderr = process.communicate(timeout=5)

            self.assertEqual(process.returncode, expected_exit, stdout + stderr)
            self.assertEqual(term_log.read_text(encoding='utf-8'), 'TERM\n')
            self.assertEqual(self.attempts(), 1)
            self.assertEqual(self.sleeps(), [])
            self.assertIn(f'received {signal_name}', stderr)
            self.assertIn('forwarded SIGTERM', stderr)
            with self.assertRaises(ProcessLookupError):
                os.kill(descendant_pid, 0)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)

    def test_parent_interrupt_is_forwarded_without_a_retry(self):
        self.assert_parent_signal_is_forwarded(signal.SIGINT, 130)

    def test_parent_termination_is_forwarded_without_a_retry(self):
        self.assert_parent_signal_is_forwarded(signal.SIGTERM, 143)

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
