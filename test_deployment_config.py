import os
import re
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).parent


ACTION_PINS = {
    'actions/checkout': ('df4cb1c069e1874edd31b4311f1884172cec0e10', 'v6.0.3'),
    'actions/setup-python': ('ece7cb06caefa5fff74198d8649806c4678c61a1', 'v6.3.0'),
    'actions/configure-pages': ('45bfe0192ca1faeb007ade9deae92b16b8254a0d', 'v6.0.0'),
    'actions/upload-pages-artifact': ('fc324d3547104276b827a68afc52ff2a11cc49c9', 'v5.0.0'),
    'actions/deploy-pages': ('cd2ce8fcbc39b97be8ca5fce6e763baed58fa128', 'v5.0.0'),
}


class DeploymentConfigurationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = (ROOT / '.github/workflows/update.yml').read_text(encoding='utf-8')
        cls.watchdog = (ROOT / '.github/workflows/watchdog.yml').read_text(encoding='utf-8')
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
            'fetch-depth: 2',
            'python -m unittest',
            '--manifest snapshot_manifest.json',
            'git cat-file -e HEAD^:articles_index.json',
            'git cat-file -e HEAD^:trades_extracted.json',
            'git cat-file -e HEAD^:snapshot_manifest.json',
            '--previous-articles',
            '--previous-trades',
            '--previous-manifest',
        ):
            self.assertIn(required, self.workflow)

    def test_build_embeds_provenance_and_enforces_artifact_policy(self):
        for required in (
            'SITE_OUTPUT_DIR:',
            'SITE_REVISION: ${{ github.sha }}',
            'python validate_inline_scripts.py _site/index.html',
            'test -f _site/index.html',
            'test -f _site/article_briefs.json',
            'test -f _site/observations.json',
            'test -f _site/robots.txt',
            'test -f _site/sitemap.xml',
            'test -f _site/site.webmanifest',
            'test -f _site/favicon.svg',
            'test -f _site/og.jpg',
            'test ! -L _site/article_briefs.json',
            'test ! -L _site/observations.json',
            'artifact_file_count=$(find _site -type f',
            'artifact_file_count != 8',
            'must contain exactly the three application assets and five launch support assets',
            'find _site -type l',
            'index_bytes < 100000',
            'index_bytes > 900000',
            "brief_bytes=$(wc -c < _site/article_briefs.json",
            "observation_bytes=$(wc -c < _site/observations.json",
            'gzip_bytes=$(gzip -9 -c _site/index.html',
            'gzip_bytes > 250000',
            'brief_bytes < 100000',
            'brief_bytes > 800000',
            'observation_bytes < 500000',
            'observation_bytes > 1500000',
            'total_bytes > 3000000',
            "Path('_site/article_briefs.json').read_text",
            "Path('_site/observations.json').read_text",
            "deferred.get('schema_version') != 1",
            "deferred.get('data_checksum') != expected_checksum",
            "not isinstance(deferred.get('briefs'), dict)",
            "observation_asset.get('schema_version') != 1",
            "observation_asset.get('data_checksum') != expected_checksum",
            "not isinstance(observation_asset.get('observations'), list)",
            'len(asset_observations) != len(source_observations)',
            'Deferred observation identities do not match the source snapshot.',
            'Deferred observation content differs from the source snapshot.',
            'from smoke_test_site import snapshot_checksum, validate_html',
            'id: asset_hashes',
            'html_sha256=$(sha256sum _site/index.html',
            'brief_sha256=$(sha256sum _site/article_briefs.json',
            'observation_sha256=$(sha256sum _site/observations.json',
            'support_sha256=$(python -c',
            "support_bundle_checksum('_site')",
            'html_sha256: ${{ steps.asset_hashes.outputs.html_sha256 }}',
            'brief_sha256: ${{ steps.asset_hashes.outputs.brief_sha256 }}',
            'observation_sha256: ${{ steps.asset_hashes.outputs.observation_sha256 }}',
            'support_sha256: ${{ steps.asset_hashes.outputs.support_sha256 }}',
            'retention-days: 7',
        ):
            self.assertIn(required, self.workflow)

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
            '--expected-html-sha256 "$EXPECTED_HTML_SHA256"',
            '--expected-brief-sha256 "$EXPECTED_BRIEF_SHA256"',
            '--expected-observation-sha256 "$EXPECTED_OBSERVATION_SHA256"',
            '--expected-support-sha256 "$EXPECTED_SUPPORT_SHA256"',
            '--retries 8',
        ):
            self.assertIn(required, deploy_job)
        self.assertIn('contents: read', deploy_job)
        self.assertIn('persist-credentials: false', deploy_job)

    def test_watchdog_verifies_exact_release_and_enforces_sixteen_hour_freshness(self):
        for required in (
            "cron: '17 */4 * * *'",
            'workflow_dispatch:',
            'group: published-research-watchdog',
            'cancel-in-progress: true',
            'timeout-minutes: 5',
            'persist-credentials: false',
            'smoke_test_site.py',
            '--expected-revision "$GITHUB_SHA"',
            '--articles-file articles_index.json',
            '--observations-file trades_extracted.json',
            'Rebuild trusted asset fingerprints',
            'SITE_OUTPUT_DIR: ${{ runner.temp }}/expected-site',
            'SITE_REVISION: ${{ github.sha }}',
            'html_sha256=$(sha256sum "$SITE_OUTPUT_DIR/index.html"',
            'brief_sha256=$(sha256sum "$SITE_OUTPUT_DIR/article_briefs.json"',
            'observation_sha256=$(sha256sum "$SITE_OUTPUT_DIR/observations.json"',
            'support_sha256=$(SITE_OUTPUT_DIR="$SITE_OUTPUT_DIR" python3 -c',
            '--expected-html-sha256 "$EXPECTED_HTML_SHA256"',
            '--expected-brief-sha256 "$EXPECTED_BRIEF_SHA256"',
            '--expected-observation-sha256 "$EXPECTED_OBSERVATION_SHA256"',
            '--expected-support-sha256 "$EXPECTED_SUPPORT_SHA256"',
            '--retries 2',
            'https://navnoorthapar.github.io/substack-trades/',
            "json.load(open('snapshot_manifest.json'",
            "snapshot['checked_at']",
            'datetime.now(timezone.utc)',
            'timedelta(minutes=10)',
            'implausibly far in the future',
            "snapshot.get('sources', {}).items()",
            'age.total_seconds() > 16 * 3600',
        ):
            self.assertIn(required, self.watchdog)
        self.assertRegex(self.watchdog, r'(?m)^permissions:\n\s+contents: read$')
        self.assertNotIn('contents: write', self.watchdog)
        self.assertNotRegex(self.watchdog, r'(?m)^\s*run:\s*git (?:commit|push)')
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
        self.assertIn(
            "cancel-in-progress: ${{ github.event_name == 'pull_request' }}",
            self.workflow,
        )

    def test_dependabot_checks_github_actions_weekly(self):
        self.assertRegex(self.dependabot, r'(?m)^version: 2$')
        self.assertIn('package-ecosystem: github-actions', self.dependabot)
        self.assertIn('interval: weekly', self.dependabot)
        self.assertIn('timezone: Asia/Kolkata', self.dependabot)

    def test_generated_site_is_untracked_and_refresh_commits_only_source_data(self):
        self.assertIn('/docs/', self.ignore)
        self.assertIn('/_site/', self.ignore)
        self.assertNotIn('git add docs', self.refresh)
        self.assertIn('-m unittest', self.refresh)
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

    def test_refresh_rolls_back_promoted_snapshot_before_any_git_staging(self):
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
        accepted_at = self.refresh.index('PROMOTION_ACTIVE=0', regression_at)
        git_stage_at = self.refresh.index('git add -- "${TRACKED_OUTPUTS[@]}"')
        self.assertLess(cache_candidate_at, validate_at)
        self.assertLess(validate_at, backup_at)
        self.assertLess(backup_at, promote_at)
        self.assertLess(promote_at, regression_at)
        self.assertLess(regression_at, accepted_at)
        self.assertLess(accepted_at, git_stage_at)
        self.assertLess(regression_at, git_stage_at)

    def test_transaction_backups_are_removed_by_cleanup(self):
        cleanup = self.refresh.split('cleanup() {', 1)[1].split('\n}', 1)[0]
        self.assertIn('rm -f "$WORK_DIR"/*.json', cleanup)
        self.assertIn('rm -f "$WORK_DIR"/*.tmp', cleanup)
        self.assertIn('rm -f "$WORK_DIR"/*.previous-missing', cleanup)


if __name__ == '__main__':
    unittest.main()
