import os
import re
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).parent


ACTION_PINS = {
    'actions/checkout': ('3d3c42e5aac5ba805825da76410c181273ba90b1', 'v7.0.1'),
    'actions/setup-python': ('5fda3b95a4ea91299a34e894583c3862153e4b97', 'v7.0.0'),
    'actions/configure-pages': ('45bfe0192ca1faeb007ade9deae92b16b8254a0d', 'v6.0.0'),
    'actions/upload-pages-artifact': ('fc324d3547104276b827a68afc52ff2a11cc49c9', 'v5.0.0'),
    'actions/deploy-pages': ('cd2ce8fcbc39b97be8ca5fce6e763baed58fa128', 'v5.0.0'),
}


class DeploymentConfigurationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = (ROOT / '.github/workflows/update.yml').read_text(encoding='utf-8')
        cls.watchdog = (ROOT / '.github/workflows/watchdog.yml').read_text(encoding='utf-8')
        cls.rollback = (ROOT / '.github/workflows/rollback.yml').read_text(encoding='utf-8')
        cls.dependabot = (ROOT / '.github/dependabot.yml').read_text(encoding='utf-8')
        cls.refresh = (ROOT / 'refresh.sh').read_text(encoding='utf-8')
        cls.automation_status = (ROOT / 'automation_status.sh').read_text(encoding='utf-8')
        cls.ignore = (ROOT / '.gitignore').read_text(encoding='utf-8').splitlines()

    def run_automation_status(self, launchctl_output):
        with tempfile.TemporaryDirectory() as directory:
            test_root = Path(directory)
            home = test_root / 'home'
            fake_bin = test_root / 'bin'
            home.mkdir()
            fake_bin.mkdir()
            (home / '.substack_trades_last_run').write_text(
                f'{int(time.time())}\n', encoding='utf-8',
            )

            launchctl = fake_bin / 'launchctl'
            launchctl.write_text(
                '#!/bin/sh\n'
                'if [ "$1" = "print" ]; then\n'
                '    printf \'%s\\n\' "${FAKE_LAUNCHCTL_OUTPUT-}"\n'
                '    exit "${FAKE_LAUNCHCTL_STATUS-0}"\n'
                'fi\n'
                'exit 2\n',
                encoding='utf-8',
            )
            launchctl.chmod(0o755)

            gh = fake_bin / 'gh'
            gh.write_text(
                '#!/bin/sh\n'
                'case "$1" in\n'
                '    api) printf \'workflow\\n\' ;;\n'
                '    run) printf \'completed|success|4242\\n\' ;;\n'
                '    *) exit 2 ;;\n'
                'esac\n',
                encoding='utf-8',
            )
            gh.chmod(0o755)

            environment = os.environ.copy()
            environment.update({
                'FAKE_LAUNCHCTL_OUTPUT': launchctl_output,
                'HOME': str(home),
                'MAX_AGE_SECONDS': '57600',
                'PATH': f'{fake_bin}:{environment.get("PATH", "")}',
            })
            return subprocess.run(
                ['/bin/bash', str(ROOT / 'automation_status.sh')],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )

    def test_automation_status_accepts_a_successful_latest_updater_exit(self):
        result = self.run_automation_status(
            'state = not running\nruns = 3\nlast exit code = 0',
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Updater: loaded', result.stdout)
        self.assertIn('Updater last exit: successful', result.stdout)
        self.assertIn('Latest deployment: successful (run 4242)', result.stdout)

    def test_automation_status_rejects_a_failed_latest_updater_exit(self):
        result = self.run_automation_status(
            'state = not running\nruns = 4\nlast exit code = 78',
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('Updater last exit: FAILED (code 78)', result.stdout)
        self.assertIn('Inspect updater errors:', result.stdout)
        self.assertIn('launchctl kickstart -k', result.stdout)
        self.assertNotIn('Repair updater with:', result.stdout)

    def test_automation_status_rejects_missing_latest_exit_evidence(self):
        result = self.run_automation_status('state = waiting\nruns = 0')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(
            'Updater last exit: unavailable (no completed run recorded)',
            result.stdout,
        )
        self.assertIn('Inspect updater errors:', result.stdout)

    def test_every_third_party_action_is_immutable_and_version_annotated(self):
        for label, workflow, checkout_count in (
            ('deployment', self.workflow, 2),
            ('watchdog', self.watchdog, 1),
            ('rollback', self.rollback, 1),
        ):
            action_uses = re.findall(
                r'(?m)^\s*uses:\s*([^@\s]+)@([^\s]+)(?:\s+#\s*(\S+))?$',
                workflow,
            )
            self.assertTrue(action_uses, f'{label} workflow has no pinned actions')
            for action, revision, version in action_uses:
                self.assertRegex(revision, r'^[0-9a-f]{40}$')
                self.assertIn(action, ACTION_PINS)
                expected_revision, expected_version = ACTION_PINS[action]
                self.assertEqual(revision, expected_revision)
                self.assertEqual(version, expected_version)
            self.assertEqual(
                sum(action == 'actions/checkout' for action, _, _ in action_uses),
                checkout_count,
            )

    def test_quality_gate_validates_current_and_available_prior_snapshot(self):
        for required in (
            'fetch-depth: 0',
            'python -m unittest',
            '--manifest snapshot_manifest.json',
            'github.event.before',
            'github.event.pull_request.base.sha',
            'git cat-file -e "$BASELINE_SHA:articles_index.json"',
            'git cat-file -e "$BASELINE_SHA:trades_extracted.json"',
            'git cat-file -e "$BASELINE_SHA:snapshot_manifest.json"',
            '--previous-articles',
            '--previous-trades',
            '--previous-manifest',
        ):
            self.assertIn(required, self.workflow)
        self.assertNotIn('HEAD^:', self.workflow)

    def test_build_uses_one_shared_release_validator_and_fingerprints(self):
        for required in (
            'SITE_OUTPUT_DIR:',
            'SITE_REVISION: ${{ github.sha }}',
            'python build_site.py',
            'python validate_inline_scripts.py _site/index.html',
            '- name: Validate and fingerprint exact release artifact',
            'id: release',
            'python validate_release.py',
            '--site _site',
            '--articles articles_index.json',
            '--trades trades_extracted.json',
            '--manifest snapshot_manifest.json',
            '--expected-revision "${{ github.sha }}"',
            '--github-output "$GITHUB_OUTPUT"',
            'html_sha256: ${{ steps.release.outputs.html_sha256 }}',
            'brief_sha256: ${{ steps.release.outputs.brief_sha256 }}',
            'observation_sha256: ${{ steps.release.outputs.observation_sha256 }}',
            'support_sha256: ${{ steps.release.outputs.support_sha256 }}',
            'data_sha256: ${{ steps.release.outputs.data_sha256 }}',
            'share_sha256: ${{ steps.release.outputs.share_sha256 }}',
            'retention-days: 7',
        ):
            self.assertIn(required, self.workflow)
        for removed_duplicate in (
            'Enforce artifact integrity and size policy',
            'Record trusted release fingerprints',
            'id: asset_hashes',
            'Generated article observation references are invalid.',
            'html_sha256=$(sha256sum',
        ):
            self.assertNotIn(removed_duplicate, self.workflow)
        self.assertNotRegex(
            self.workflow,
            r'(?i)grep[^\n]*(?:subscriber|revenue|pledge|open.?rate)',
        )
        build_at = self.workflow.index('python build_site.py')
        compile_at = self.workflow.index(
            'python validate_inline_scripts.py _site/index.html', build_at,
        )
        release_at = self.workflow.index(
            'python validate_release.py', compile_at,
        )
        upload_at = self.workflow.index(
            '- name: Upload Pages artifact', release_at,
        )
        self.assertLess(build_at, compile_at)
        self.assertLess(compile_at, release_at)
        self.assertLess(release_at, upload_at)

    def test_release_inputs_stay_exact_and_stale_main_runs_cannot_deploy(self):
        for required in (
            '- name: Prove tests did not mutate release inputs',
            '- name: Prove build kept release inputs immutable',
            'test "$(git rev-parse --verify HEAD)" = "$GITHUB_SHA"',
            'git status --porcelain --untracked-files=normal',
            'currency:',
            'name: Confirm current production revision',
            'id: remote',
            'git ls-remote --exit-code "$REMOTE_URL" refs/heads/main',
            'for attempt in 1 2 3',
            'if [ "$remote_main" != "$GITHUB_SHA" ]',
            'echo "current=false" >> "$GITHUB_OUTPUT"',
            'Superseded release skipped',
            "needs.currency.outputs.current == 'true'",
        ):
            self.assertIn(required, self.workflow)
        self.assertEqual(
            self.workflow.count(
                'test "$(git rev-parse --verify HEAD)" = "$GITHUB_SHA"'
            ),
            2,
        )
        tests_at = self.workflow.index('python -m unittest')
        prebuild_clean_at = self.workflow.index(
            '- name: Prove tests did not mutate release inputs'
        )
        build_at = self.workflow.index('python build_site.py')
        release_at = self.workflow.index('python validate_release.py')
        postbuild_clean_at = self.workflow.index(
            '- name: Prove build kept release inputs immutable'
        )
        upload_at = self.workflow.index('- name: Upload Pages artifact')
        currency_at = self.workflow.index('currency:')
        remote_at = self.workflow.index('- name: Compare release with remote main')
        deploy_at = self.workflow.index('- name: Deploy GitHub Pages artifact')
        self.assertLess(tests_at, prebuild_clean_at)
        self.assertLess(prebuild_clean_at, build_at)
        self.assertLess(release_at, postbuild_clean_at)
        self.assertLess(postbuild_clean_at, upload_at)
        self.assertLess(currency_at, remote_at)
        self.assertLess(remote_at, deploy_at)

    def test_verified_rollback_archive_and_manual_restore_are_fail_closed(self):
        for required in (
            'python rollback_bundle.py create',
            '--revision "${{ github.sha }}"',
            '- name: Revalidate copied rollback bundle',
            'python rollback_bundle.py validate',
            '--bundle "$RUNNER_TEMP/verified-pages"',
            'name: verified-pages-${{ github.sha }}',
            'path: ${{ runner.temp }}/verified-pages',
            'retention-days: 90',
        ):
            self.assertIn(required, self.workflow)
        prepare_at = self.workflow.index(
            '- name: Prepare verified emergency rollback bundle'
        )
        revalidate_at = self.workflow.index(
            '- name: Revalidate copied rollback bundle'
        )
        pages_upload_at = self.workflow.index('- name: Upload Pages artifact')
        archive_upload_at = self.workflow.index(
            '- name: Retain verified rollback bundle'
        )
        self.assertLess(prepare_at, revalidate_at)
        self.assertLess(revalidate_at, pages_upload_at)
        self.assertLess(revalidate_at, archive_upload_at)
        for required in (
            'name: Emergency Pages Rollback',
            'workflow_dispatch:',
            "if: github.ref == 'refs/heads/main'",
            'group: pages-production',
            'queue: max',
            'actions: read',
            'contents: read',
            'pages: write',
            'id-token: write',
            'CONFIRMATION: ${{ inputs.confirmation }}',
            '[ "$CONFIRMATION" = "ROLLBACK" ]',
            '- name: Prove rollback tooling is current main',
            'git ls-remote --exit-code origin refs/heads/main',
            '[ "$GITHUB_SHA" = "$remote_main" ]',
            'actions/workflows/update.yml',
            'actions/runs/$SOURCE_RUN_ID',
            '[[ "$event" = "push" || "$event" = "workflow_dispatch" ]]',
            '[ "$status" = "completed" ]',
            '[ "$conclusion" = "success" ]',
            '[ "$head_branch" = "main" ]',
            '[ "$head_sha" = "$RELEASE_SHA" ]',
            '--name "verified-pages-$RELEASE_SHA"',
            'python rollback_bundle.py extract',
            '- name: Verify schema-neutral artifact attestation',
            'python rollback_bundle.py verify-attestation',
            '- name: Refuse superseded rollback tooling',
            'path: ${{ runner.temp }}/verified-rollback/site',
            'actions/deploy-pages@',
            'python rollback_bundle.py smoke',
            '--base-url "$DEPLOYED_URL"',
            '--expected-revision "$EXPECTED_REVISION"',
            '--concurrency 12',
        ):
            self.assertIn(required, self.rollback)
        self.assertNotRegex(self.rollback, r'(?m)^\s*(?:push|schedule):')
        for forbidden in (
            'release-source',
            'ref: ${{ inputs.release_sha }}',
            'smoke_test_site.py',
        ):
            self.assertNotIn(forbidden, self.rollback)
        main_at = self.rollback.index(
            '- name: Prove rollback tooling is current main'
        )
        authority_at = self.rollback.index('- name: Verify source run authority')
        validate_at = self.rollback.index(
            '- name: Verify schema-neutral artifact attestation'
        )
        current_again_at = self.rollback.index(
            '- name: Refuse superseded rollback tooling'
        )
        upload_at = self.rollback.index(
            '- name: Upload exact rollback Pages artifact'
        )
        deploy_at = self.rollback.index('- name: Deploy verified rollback')
        smoke_at = self.rollback.index('- name: Verify exact rollback is live')
        self.assertLess(main_at, authority_at)
        self.assertLess(authority_at, validate_at)
        self.assertLess(validate_at, upload_at)
        self.assertLess(upload_at, current_again_at)
        self.assertLess(current_again_at, deploy_at)
        self.assertLess(upload_at, deploy_at)
        self.assertLess(deploy_at, smoke_at)

    def test_post_deploy_smoke_verifies_exact_live_release(self):
        deploy_job = self.workflow.split('\n  deploy:', 1)[1]
        for required in (
            'smoke_test_site.py',
            '${{ steps.deployment.outputs.page_url }}',
            '--expected-revision "$EXPECTED_REVISION"',
            '--articles-file articles_index.json',
            '--observations-file trades_extracted.json',
            'EXPECTED_HTML_SHA256: ${{ needs.quality.outputs.html_sha256 }}',
            'EXPECTED_BRIEF_SHA256: ${{ needs.quality.outputs.brief_sha256 }}',
            'EXPECTED_OBSERVATION_SHA256: ${{ needs.quality.outputs.observation_sha256 }}',
            'EXPECTED_SUPPORT_SHA256: ${{ needs.quality.outputs.support_sha256 }}',
            'EXPECTED_DATA_SHA256: ${{ needs.quality.outputs.data_sha256 }}',
            'EXPECTED_SHARE_SHA256: ${{ needs.quality.outputs.share_sha256 }}',
            '--expected-html-sha256 "$EXPECTED_HTML_SHA256"',
            '--expected-brief-sha256 "$EXPECTED_BRIEF_SHA256"',
            '--expected-observation-sha256 "$EXPECTED_OBSERVATION_SHA256"',
            '--expected-support-sha256 "$EXPECTED_SUPPORT_SHA256"',
            '--expected-data-sha256 "$EXPECTED_DATA_SHA256"',
            '--expected-share-sha256 "$EXPECTED_SHARE_SHA256"',
            '--retries 8',
            '- name: Publish production incident guidance',
            "if: failure() && steps.deployment.outcome == 'success'",
            'Follow LAUNCH_RUNBOOK.md',
        ):
            self.assertIn(required, deploy_job)
        self.assertIn('contents: read', deploy_job)
        self.assertIn('persist-credentials: false', deploy_job)

    def test_watchdog_verifies_exact_release_and_enforces_sixteen_hour_freshness(self):
        for required in (
            "cron: '17 */4 * * *'",
            'workflow_dispatch:',
            'group: published-research-watchdog',
            'queue: max',
            'timeout-minutes: 12',
            'persist-credentials: false',
            'Confirm the expected release still owns main',
            'id: currency',
            'Superseded watchdog skipped',
            'smoke_test_site.py',
            '--expected-revision "$GITHUB_SHA"',
            '--articles-file articles_index.json',
            '--observations-file trades_extracted.json',
            'Rebuild and validate trusted release',
            'id: release',
            'SITE_OUTPUT_DIR: ${{ runner.temp }}/expected-site',
            'SITE_REVISION: ${{ github.sha }}',
            'python3 validate_release.py',
            '--site "$SITE_OUTPUT_DIR"',
            '--expected-revision "$SITE_REVISION"',
            '--github-output "$GITHUB_OUTPUT"',
            'EXPECTED_HTML_SHA256: ${{ steps.release.outputs.html_sha256 }}',
            'EXPECTED_BRIEF_SHA256: ${{ steps.release.outputs.brief_sha256 }}',
            'EXPECTED_OBSERVATION_SHA256: ${{ steps.release.outputs.observation_sha256 }}',
            'EXPECTED_SUPPORT_SHA256: ${{ steps.release.outputs.support_sha256 }}',
            'EXPECTED_DATA_SHA256: ${{ steps.release.outputs.data_sha256 }}',
            'EXPECTED_SHARE_SHA256: ${{ steps.release.outputs.share_sha256 }}',
            '--expected-html-sha256 "$EXPECTED_HTML_SHA256"',
            '--expected-brief-sha256 "$EXPECTED_BRIEF_SHA256"',
            '--expected-observation-sha256 "$EXPECTED_OBSERVATION_SHA256"',
            '--expected-support-sha256 "$EXPECTED_SUPPORT_SHA256"',
            '--expected-data-sha256 "$EXPECTED_DATA_SHA256"',
            '--expected-share-sha256 "$EXPECTED_SHARE_SHA256"',
            'Verify exact live release with bounded propagation retries',
            'id: smoke',
            'continue-on-error: true',
            '--retries 20',
            '--retry-delay 20',
            '--timeout 10',
            'https://navnoorthapar.github.io/substack-trades/',
            'Reconcile a failed check with current main',
            "steps.smoke.outcome == 'failure'",
            'Superseded watchdog reconciled',
            'Published release verification failed',
            "steps.smoke.outcome == 'success'",
            "json.load(open('snapshot_manifest.json'",
            "snapshot['checked_at']",
            'datetime.now(timezone.utc)',
            'timedelta(minutes=10)',
            'implausibly far in the future',
            "snapshot.get('sources', {}).items()",
            'maximum_age = timedelta(hours=16)',
            'maximum_source_lag = timedelta(hours=1)',
            'later than the snapshot manifest',
            'too far behind the snapshot manifest',
            'source check is stale',
            'source_age > maximum_age',
            'age > maximum_age',
        ):
            self.assertIn(required, self.watchdog)
        self.assertRegex(self.watchdog, r'(?m)^permissions:\n\s+contents: read$')
        self.assertNotIn('contents: write', self.watchdog)
        self.assertNotRegex(self.watchdog, r'(?m)^\s*run:\s*git (?:commit|push)')
        retries = int(re.search(r'--retries (\d+)', self.watchdog).group(1))
        backoff = int(re.search(r'--retry-delay (\d+)', self.watchdog).group(1))
        request_timeout = int(re.search(r'--timeout (\d+)', self.watchdog).group(1))
        job_minutes = int(re.search(r'timeout-minutes: (\d+)', self.watchdog).group(1))
        self.assertGreaterEqual((retries - 1) * backoff, 6 * 60)
        self.assertLessEqual(
            retries * request_timeout + (retries - 1) * backoff,
            job_minutes * 60,
        )
        self.assertIn('MAX_AGE_SECONDS=${MAX_AGE_SECONDS:-57600}', self.automation_status)
        self.assertNotIn('129600', self.automation_status)

    def test_pull_requests_cannot_deploy(self):
        self.assertRegex(self.workflow, r'(?m)^\s*push:')
        self.assertRegex(self.workflow, r'(?m)^\s*pull_request:')
        self.assertNotRegex(self.workflow, r'(?m)^\s*paths:')
        self.assertGreaterEqual(self.workflow.count("github.event_name != 'pull_request'"), 3)
        self.assertIn("github.ref == 'refs/heads/main'", self.workflow)

    def test_permissions_are_least_privilege_and_checkout_cannot_push(self):
        self.assertRegex(self.workflow, r'(?m)^permissions: \{\}$')
        self.assertEqual(self.workflow.count('persist-credentials: false'), 2)
        self.assertIn('contents: read', self.workflow)
        self.assertIn('pages: write', self.workflow)
        self.assertIn('id-token: write', self.workflow)
        self.assertNotIn('contents: write', self.workflow)
        self.assertNotRegex(self.workflow, r'(?m)^\s*run:\s*git (?:commit|push)')

    def test_production_deployments_are_serialized_without_cancellation(self):
        self.assertIn("|| 'production'", self.workflow)
        self.assertIn('queue: max', self.workflow)
        self.assertNotIn('cancel-in-progress:', self.workflow)
        self.assertIn('queue: max', self.rollback)
        self.assertNotIn('cancel-in-progress:', self.rollback)
        self.assertIn('name: Confirm current production revision', self.workflow)
        self.assertIn('echo "current=false" >> "$GITHUB_OUTPUT"', self.workflow)
        self.assertIn("needs.currency.outputs.current == 'true'", self.workflow)

    def test_dependabot_checks_github_actions_weekly(self):
        self.assertRegex(self.dependabot, r'(?m)^version: 2$')
        self.assertIn('package-ecosystem: github-actions', self.dependabot)
        self.assertIn('interval: weekly', self.dependabot)
        self.assertIn('timezone: Asia/Kolkata', self.dependabot)
        self.assertIn('open-pull-requests-limit: 0', self.dependabot)

    def test_quality_tool_install_retries_transient_registry_failures(self):
        self.assertIn('- name: Install pinned quality tools', self.workflow)
        self.assertIn('for attempt in 1 2 3', self.workflow)
        self.assertIn('mypy==1.11.2 ruff==0.12.4', self.workflow)
        self.assertIn('Quality tool download retry', self.workflow)
        self.assertIn('Quality tool installation failed', self.workflow)

    def test_generated_site_is_untracked_and_refresh_commits_only_source_data(self):
        self.assertIn('/docs/', self.ignore)
        self.assertIn('/_site/', self.ignore)
        self.assertNotIn('git add docs', self.refresh)
        self.assertIn('-m unittest', self.refresh)
        self.assertIn('"$PYTHON" validate_release.py', self.refresh)
        self.assertIn('--expected-revision scheduled-refresh-candidate', self.refresh)
        self.assertIn('TRACKED_OUTPUTS=(', self.refresh)
        self.assertIn('git add -- "${TRACKED_OUTPUTS[@]}"', self.refresh)
        self.assertIn('git diff --staged --quiet -- "${TRACKED_OUTPUTS[@]}"', self.refresh)
        self.assertRegex(
            self.refresh,
            r'git commit --only[\s\S]*-- "\$\{TRACKED_OUTPUTS\[@\]\}"',
        )

    def test_scheduled_refresh_fails_closed_on_dirty_source_and_retries_push(self):
        for required in (
            'CURRENT_BRANCH=$(git branch --show-current)',
            'if [ "$CURRENT_BRANCH" != "main" ]',
            'git status --porcelain --untracked-files=normal',
            'git pull --ff-only origin main',
            'for attempt in 1 2 3',
            'push_succeeded=0',
            'git push origin main',
            'failed; retrying in ${retry_delay}s',
        ):
            self.assertIn(required, self.refresh)
        self.assertNotIn('--autostash', self.refresh)
        branch_gate_at = self.refresh.index('CURRENT_BRANCH=$(git branch --show-current)')
        clean_gate_at = self.refresh.index(
            'git status --porcelain --untracked-files=normal'
        )
        pull_at = self.refresh.index('git pull --ff-only origin main')
        self.assertLess(branch_gate_at, clean_gate_at)
        self.assertLess(clean_gate_at, pull_at)

    def test_refresh_rolls_back_until_commit_then_retries_push_without_rollback(self):
        for required in (
            'DIRECTION_CACHE_CANDIDATE="$WORK_DIR/direction-cache.candidate.json"',
            'cp -p "$ROOT/.direction_cache.json" "$DIRECTION_CACHE_CANDIDATE"',
            'DIRECTION_CACHE_PATH="$DIRECTION_CACHE_CANDIDATE"',
            'PROMOTED_OUTPUTS=(',
            '.direction_cache.json',
            'PROMOTION_CANDIDATES=(',
            '"$WORK_DIR/promoted-$index.previous.json"',
            '"$WORK_DIR/promoted-$index.previous-missing"',
            'PROMOTION_ACTIVE=1',
            'restore_promoted_outputs()',
            'if [ "$PROMOTION_ACTIVE" -eq 1 ]',
            'if ! "$PYTHON" -m unittest -q; then',
            'Regression suite failed; restoring the previous local snapshot.',
            'SITE_REVISION=scheduled-refresh-candidate',
            '"$PYTHON" validate_release.py',
            'GIT_PUBLICATION_ACTIVE=1',
            'git reset --quiet HEAD --',
            'mv "$previous" "$ROOT/$output"',
            'rm -f "$ROOT/$output"',
        ):
            self.assertIn(required, self.refresh)

        cache_candidate_at = self.refresh.index(
            'DIRECTION_CACHE_CANDIDATE="$WORK_DIR/direction-cache.candidate.json"'
        )
        validate_at = self.refresh.index('"$PYTHON" validate_pipeline.py')
        backup_at = self.refresh.index('PROMOTED_OUTPUTS=(', validate_at)
        promote_at = self.refresh.index('PROMOTION_ACTIVE=1', backup_at)
        regression_at = self.refresh.index('if ! "$PYTHON" -m unittest -q; then')
        release_at = self.refresh.index(
            '"$PYTHON" validate_release.py', regression_at,
        )
        git_stage_at = self.refresh.index('git add -- "${TRACKED_OUTPUTS[@]}"')
        git_commit_at = self.refresh.index('git commit --only', git_stage_at)
        accepted_at = self.refresh.index('PROMOTION_ACTIVE=0', git_commit_at)
        push_at = self.refresh.index('git push origin main', accepted_at)
        self.assertLess(cache_candidate_at, validate_at)
        self.assertLess(validate_at, backup_at)
        self.assertLess(backup_at, promote_at)
        self.assertLess(promote_at, regression_at)
        self.assertLess(regression_at, release_at)
        self.assertLess(release_at, git_stage_at)
        self.assertLess(git_stage_at, git_commit_at)
        self.assertLess(git_commit_at, accepted_at)
        self.assertLess(accepted_at, push_at)

    def test_transaction_backups_are_removed_by_cleanup(self):
        cleanup = self.refresh.split('cleanup() {', 1)[1].split('\n}', 1)[0]
        self.assertIn('rm -r "$RELEASE_SITE_DIR"', cleanup)
        self.assertIn('rm -f "$WORK_DIR"/*.json', cleanup)
        self.assertIn('rm -f "$WORK_DIR"/*.tmp', cleanup)
        self.assertIn('rm -f "$WORK_DIR"/*.previous-missing', cleanup)


if __name__ == '__main__':
    unittest.main()
