"""Executable tests for the exact-revision release and pre-push gates."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parent
RELEASE_GATE = ROOT / 'release_gate.sh'
PRE_PUSH = ROOT / '.githooks' / 'pre-push'
INSTALL_AUTOMATION = ROOT / 'install_automation.sh'
ZERO_OID = '0' * 40


class ReleaseGateTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.test_root = Path(self.temporary_directory.name)
        self.fake_bin = self.test_root / 'bin'
        self.temp_dir = self.test_root / 'tmp'
        self.fake_bin.mkdir()
        self.temp_dir.mkdir()
        self.command_log = self.test_root / 'commands.log'
        self.python_log = self.test_root / 'python.log'
        self.revision = 'a' * 40
        self.write_fake_release_tools()

    def executable(self, path, content):
        path.write_text(content, encoding='utf-8')
        path.chmod(0o755)

    def write_fake_release_tools(self):
        self.executable(
            self.fake_bin / 'git',
            '#!/bin/bash\n'
            'set -eu\n'
            'printf \'git\\t%s\\n\' "$*" >> "$FAKE_COMMAND_LOG"\n'
            'if [ "$1" = "rev-parse" ] && [ "$2" = "--verify" ]; then\n'
            '    if [ "$3" = "HEAD" ]; then\n'
            '        printf \'%s\\n\' "$FAKE_HEAD_SHA"\n'
            '    else\n'
            '        printf \'%s\\n\' "$FAKE_TARGET_SHA"\n'
            '    fi\n'
            'elif [ "$1" = "status" ]; then\n'
            '    status_count_file="${FAKE_STATUS_COUNT_FILE-}"\n'
            '    status_count=1\n'
            '    if [ -n "$status_count_file" ] && [ -f "$status_count_file" ]; then\n'
            '        status_count=$(( $(cat "$status_count_file") + 1 ))\n'
            '    fi\n'
            '    if [ -n "$status_count_file" ]; then\n'
            '        printf \'%s\\n\' "$status_count" > "$status_count_file"\n'
            '    fi\n'
            '    if [ "${FAKE_DIRTY_STATUS_CALL-}" = "$status_count" ]; then\n'
            '        printf \' M mutated-release-input.json\\n\'\n'
            '    else\n'
            '        printf \'%s\' "${FAKE_GIT_STATUS-}"\n'
            '    fi\n'
            'elif [ "$1" = "diff" ] && [ "$2" = "--check" ]; then\n'
            '    exit 0\n'
            'else\n'
            '    exit 97\n'
            'fi\n',
        )
        self.executable(
            self.fake_bin / 'fake-python',
            '#!/bin/bash\n'
            'set -eu\n'
            'printf \'python\\t%s\\t%s\\t%s\\t%s\\n\' '
            '"$PWD" "$*" "${SITE_OUTPUT_DIR-}" "${SITE_REVISION-}" '
            '>> "$FAKE_PYTHON_LOG"\n'
            'case " $* " in\n'
            '    *" build_site.py "*)\n'
            '        mkdir -p "$SITE_OUTPUT_DIR"\n'
            '        printf \'fake site\\n\' > "$SITE_OUTPUT_DIR/index.html"\n'
            '        ;;\n'
            'esac\n'
            'if [ -n "${FAKE_PYTHON_FAIL_MATCH-}" ]; then\n'
            '    case "$*" in\n'
            '        *"$FAKE_PYTHON_FAIL_MATCH"*) exit 23 ;;\n'
            '    esac\n'
            'fi\n'
            'exit 0\n',
        )
        for tool in ('ruff', 'mypy', 'plutil'):
            self.executable(
                self.fake_bin / tool,
                '#!/bin/bash\n'
                'set -eu\n'
                'printf \'%s\\t%s\\n\' "$(basename "$0")" "$*" '
                '>> "$FAKE_COMMAND_LOG"\n',
            )

    def environment(self, head_sha=None):
        environment = os.environ.copy()
        environment.update({
            'FAKE_COMMAND_LOG': str(self.command_log),
            'FAKE_PYTHON_LOG': str(self.python_log),
            'FAKE_HEAD_SHA': head_sha or self.revision,
            'FAKE_TARGET_SHA': self.revision,
            'FAKE_STATUS_COUNT_FILE': str(self.test_root / 'status-count'),
            'PATH': str(self.fake_bin) + ':' + environment.get('PATH', ''),
            'PYTHON_BIN': str(self.fake_bin / 'fake-python'),
            'TMPDIR': str(self.temp_dir),
        })
        return environment

    def run_gate(self, revision=None, environment=None):
        return subprocess.run(
            ['/bin/bash', str(RELEASE_GATE), revision or self.revision],
            cwd=ROOT,
            env=environment or self.environment(),
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

    def test_release_gate_runs_all_stages_with_exact_revision_and_override(self):
        result = self.run_gate()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        python_log = self.python_log.read_text(encoding='utf-8')
        command_log = self.command_log.read_text(encoding='utf-8')
        for required in (
            '-m unittest discover -s . -p test_*.py -v',
            'validate_pipeline.py --articles articles_index.json '
            '--trades trades_extracted.json --manifest snapshot_manifest.json',
            '-m py_compile',
            'build_site.py',
            'validate_inline_scripts.py',
            'validate_release.py --site',
            '--expected-revision ' + self.revision,
        ):
            self.assertIn(required, python_log)
        build_line = next(
            line for line in python_log.splitlines()
            if '\tbuild_site.py\t' in line
        )
        self.assertTrue(build_line.endswith('\t' + self.revision))
        for required in ('ruff\tcheck', 'mypy\t--cache-dir', 'plutil\t-lint'):
            self.assertIn(required, command_log)
        self.assertGreaterEqual(command_log.count('git\tstatus'), 3)
        self.assertIn(
            'Release gate passed for ' + self.revision,
            result.stdout,
        )
        self.assertEqual(list(self.temp_dir.iterdir()), [])

    def test_release_gate_rejects_nonexact_or_wrong_worktree_revision(self):
        short = self.run_gate('a' * 12)
        self.assertEqual(short.returncode, 2)
        self.assertIn('exact 40-character', short.stderr)

        mismatch_environment = self.environment(head_sha='b' * 40)
        mismatch = self.run_gate(environment=mismatch_environment)
        self.assertEqual(mismatch.returncode, 2)
        self.assertIn('detached worktree', mismatch.stderr)
        self.assertFalse(self.python_log.exists())

    def test_release_gate_cleans_temp_output_after_a_failed_stage(self):
        environment = self.environment()
        environment['FAKE_PYTHON_FAIL_MATCH'] = 'build_site.py'

        result = self.run_gate(environment=environment)

        self.assertEqual(result.returncode, 23, result.stdout + result.stderr)
        self.assertEqual(list(self.temp_dir.iterdir()), [])

    def test_release_gate_rejects_test_side_effect_before_build(self):
        environment = self.environment()
        environment['FAKE_DIRTY_STATUS_CALL'] = '2'

        result = self.run_gate(environment=environment)

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('unchanged clean exact-revision', result.stderr)
        python_log = self.python_log.read_text(encoding='utf-8')
        self.assertIn('-m unittest', python_log)
        self.assertFalse(any(
            '\tbuild_site.py\t' in line
            for line in python_log.splitlines()
        ))
        self.assertEqual(list(self.temp_dir.iterdir()), [])

    def test_release_gate_source_is_offline_and_has_release_artifact_checks(self):
        source = RELEASE_GATE.read_text(encoding='utf-8')
        for required in (
            'PYTHON=${PYTHON_BIN:-python3}',
            'SITE_REVISION="$REVISION"',
            '"$PYTHON" validate_release.py',
            '--articles articles_index.json',
            '--trades trades_extracted.json',
            '--manifest snapshot_manifest.json',
            '--expected-revision "$REVISION"',
            'git diff --check',
            'require_clean_revision',
            'trap cleanup EXIT',
        ):
            self.assertIn(required, source)
        for prohibited in ('gh auth', 'gh run', 'curl ', 'automation_status.sh'):
            self.assertNotIn(prohibited, source)

    def test_automation_installer_configures_hook_without_overwriting_conflicts(self):
        source = INSTALL_AUTOMATION.read_text(encoding='utf-8')
        for required in (
            '[ ! -x "$ROOT/.githooks/pre-push" ]',
            '[ ! -x "$ROOT/scheduled_refresh.sh" ]',
            'git config --local --get core.hooksPath',
            '[ "$EXISTING_HOOKS_PATH" != ".githooks" ]',
            'Refusing to replace existing Git hooks path',
            'git config --local core.hooksPath .githooks',
            'plutil -insert ProgramArguments.1 -string "$ROOT/scheduled_refresh.sh"',
            'Installed updater ProgramArguments do not match the bounded supervisor contract.',
        ):
            self.assertIn(required, source)


class PrePushGateTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.test_root = Path(self.temporary_directory.name)
        self.fake_bin = self.test_root / 'bin'
        self.fake_repo = self.test_root / 'current-worktree'
        self.temp_dir = self.test_root / 'hook-tmp'
        self.fake_bin.mkdir()
        self.fake_repo.mkdir()
        self.temp_dir.mkdir()
        self.git_log = self.test_root / 'git.log'
        self.gate_log = self.test_root / 'gate.log'
        self.write_current_worktree_gate()
        self.write_fake_git()

    def executable(self, path, content):
        path.write_text(content, encoding='utf-8')
        path.chmod(0o755)

    def write_current_worktree_gate(self):
        self.executable(
            self.fake_repo / 'release_gate.sh',
            '#!/bin/bash\n'
            'printf \'current\\t%s\\t%s\\n\' "$PWD" "$1" '
            '>> "$FAKE_GATE_LOG"\n'
            'exit 91\n',
        )

    def write_fake_git(self):
        self.executable(
            self.fake_bin / 'git',
            '#!/bin/bash\n'
            'set -eu\n'
            'printf \'%s\\n\' "$*" >> "$FAKE_GIT_LOG"\n'
            'if [ "$1" = "rev-parse" ] '
            '&& [ "$2" = "--show-toplevel" ]; then\n'
            '    printf \'%s\\n\' "$FAKE_REPO_ROOT"\n'
            '    exit 0\n'
            'fi\n'
            'if [ "$1" = "merge-base" ] '
            '&& [ "$2" = "--is-ancestor" ]; then\n'
            '    exit "${FAKE_ANCESTRY_EXIT-0}"\n'
            'fi\n'
            'if [ "$1" = "-C" ] && [ "$3" = "worktree" ] '
            '&& [ "$4" = "add" ]; then\n'
            '    checkout=$6\n'
            '    revision=$7\n'
            '    mkdir -p "$checkout"\n'
            '    cat > "$checkout/release_gate.sh" <<\'GATE\'\n'
            '#!/bin/bash\n'
            'printf \'detached\\t%s\\t%s\\t%s\\n\' '
            '"$0" "$PWD" "$1" >> "$FAKE_GATE_LOG"\n'
            'exit "${FAKE_GATE_EXIT-0}"\n'
            'GATE\n'
            '    chmod +x "$checkout/release_gate.sh"\n'
            '    printf \'%s\\n\' "$revision" > "$checkout/revision"\n'
            '    exit 0\n'
            'fi\n'
            'if [ "$1" = "-C" ] && [ "$3" = "worktree" ] '
            '&& [ "$4" = "remove" ]; then\n'
            '    checkout=$6\n'
            '    rm -rf -- "$checkout"\n'
            '    exit 0\n'
            'fi\n'
            'exit 97\n',
        )

    def environment(self, gate_exit='0', ancestry_exit='0'):
        environment = os.environ.copy()
        environment.update({
            'FAKE_GATE_EXIT': gate_exit,
            'FAKE_ANCESTRY_EXIT': ancestry_exit,
            'FAKE_GATE_LOG': str(self.gate_log),
            'FAKE_GIT_LOG': str(self.git_log),
            'FAKE_REPO_ROOT': str(self.fake_repo),
            'PATH': str(self.fake_bin) + ':' + environment.get('PATH', ''),
            'TMPDIR': str(self.temp_dir),
        })
        return environment

    def run_hook(self, updates, gate_exit='0', ancestry_exit='0'):
        return subprocess.run(
            ['/bin/bash', str(PRE_PUSH), 'origin', 'fake-remote'],
            cwd=self.fake_repo,
            env=self.environment(gate_exit, ancestry_exit),
            input=updates,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    def gate_lines(self):
        if not self.gate_log.exists():
            return []
        return self.gate_log.read_text(encoding='utf-8').splitlines()

    def test_main_target_uses_exact_local_sha_in_detached_worktree(self):
        local_sha = '1' * 40
        remote_sha = '2' * 40
        feature_sha = '3' * 40
        updates = (
            'refs/heads/feature {feature} refs/heads/feature {remote}\n'
            'refs/heads/topic {local} refs/heads/main {remote}\n'
        ).format(
            feature=feature_sha,
            local=local_sha,
            remote=remote_sha,
        )

        result = self.run_hook(updates)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        lines = self.gate_lines()
        self.assertEqual(len(lines), 1)
        mode, script, working_directory, revision = lines[0].split('\t')
        self.assertEqual(mode, 'detached')
        self.assertEqual(revision, local_sha)
        invoked_script = (Path(working_directory) / script).resolve()
        self.assertEqual(invoked_script.parent, Path(working_directory).resolve())
        self.assertTrue(
            str(invoked_script).startswith(str(self.temp_dir.resolve()))
        )
        self.assertNotIn('current', lines[0])
        git_log = self.git_log.read_text(encoding='utf-8')
        self.assertIn('worktree add --detach', git_log)
        self.assertIn(local_sha, git_log)
        self.assertNotIn('worktree add --detach', git_log.splitlines()[0])
        self.assertIn('worktree remove --force', git_log)
        self.assertEqual(list(self.temp_dir.iterdir()), [])

    def test_feature_only_skips_gate_but_main_deletion_is_rejected(self):
        feature = self.run_hook(
            'refs/heads/feature {local} refs/heads/feature {remote}\n'.format(
                local='4' * 40,
                remote='5' * 40,
            )
        )
        deletion = self.run_hook(
            '(delete) {zero} refs/heads/main {remote}\n'.format(
                zero=ZERO_OID,
                remote='6' * 40,
            )
        )

        self.assertEqual(feature.returncode, 0, feature.stderr)
        self.assertEqual(deletion.returncode, 2, deletion.stderr)
        self.assertIn('Refusing deletion of remote main', deletion.stderr)
        self.assertEqual(self.gate_lines(), [])
        self.assertFalse(self.git_log.exists())
        self.assertEqual(list(self.temp_dir.iterdir()), [])

    def test_every_nondeleted_main_update_is_validated(self):
        first = '7' * 40
        second = '8' * 40
        updates = (
            'refs/heads/one {first} refs/heads/main {remote}\n'
            'refs/heads/two {second} refs/heads/main {remote}\n'
        ).format(first=first, second=second, remote='9' * 40)

        result = self.run_hook(updates)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        revisions = [line.split('\t')[3] for line in self.gate_lines()]
        self.assertEqual(revisions, [first, second])
        self.assertEqual(list(self.temp_dir.iterdir()), [])

    def test_non_fast_forward_main_update_is_rejected_before_gate(self):
        result = self.run_hook(
            'refs/heads/main {local} refs/heads/main {remote}\n'.format(
                local='a' * 40,
                remote='b' * 40,
            ),
            ancestry_exit='1',
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn('non-fast-forward history rewrite', result.stderr)
        self.assertEqual(self.gate_lines(), [])
        self.assertIn(
            'merge-base --is-ancestor',
            self.git_log.read_text(encoding='utf-8'),
        )
        self.assertNotIn('worktree add', self.git_log.read_text(encoding='utf-8'))

    def test_missing_remote_main_revision_is_rejected(self):
        result = self.run_hook(
            'refs/heads/main {local} refs/heads/main {zero}\n'.format(
                local='a' * 40,
                zero=ZERO_OID,
            )
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn('exact existing remote revision', result.stderr)
        self.assertEqual(self.gate_lines(), [])
        self.assertFalse(self.git_log.exists())

    def test_failed_gate_blocks_push_and_still_removes_worktree(self):
        local_sha = 'c' * 40
        result = self.run_hook(
            'refs/heads/main {local} refs/heads/main {remote}\n'.format(
                local=local_sha,
                remote='d' * 40,
            ),
            gate_exit='17',
        )

        self.assertEqual(result.returncode, 17, result.stdout + result.stderr)
        self.assertEqual(
            [line.split('\t')[3] for line in self.gate_lines()],
            [local_sha],
        )
        self.assertIn(
            'worktree remove --force',
            self.git_log.read_text(encoding='utf-8'),
        )
        self.assertEqual(list(self.temp_dir.iterdir()), [])

    def test_nonexact_main_sha_is_rejected_without_worktree_substitution(self):
        result = self.run_hook(
            'refs/heads/main short refs/heads/main {remote}\n'.format(
                remote='e' * 40,
            )
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn('non-exact local commit SHA', result.stderr)
        self.assertEqual(self.gate_lines(), [])
        self.assertFalse(self.git_log.exists())
        self.assertEqual(list(self.temp_dir.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
