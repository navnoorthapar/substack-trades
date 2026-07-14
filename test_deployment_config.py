import re
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
        cls.dependabot = (ROOT / '.github/dependabot.yml').read_text(encoding='utf-8')
        cls.refresh = (ROOT / 'refresh.sh').read_text(encoding='utf-8')
        cls.ignore = (ROOT / '.gitignore').read_text(encoding='utf-8').splitlines()

    def test_every_third_party_action_is_immutable_and_version_annotated(self):
        action_uses = re.findall(
            r'(?m)^\s*uses:\s*([^@\s]+)@([^\s]+)(?:\s+#\s*(\S+))?$',
            self.workflow,
        )
        self.assertTrue(action_uses)
        for action, revision, version in action_uses:
            self.assertRegex(revision, r'^[0-9a-f]{40}$')
            self.assertIn(action, ACTION_PINS)
            expected_revision, expected_version = ACTION_PINS[action]
            self.assertEqual(revision, expected_revision)
            self.assertEqual(version, expected_version)
        self.assertEqual(sum(action == 'actions/checkout' for action, _, _ in action_uses), 2)

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
            'test -f _site/index.html',
            'find _site -type l',
            'index_bytes < 100000',
            'index_bytes > 1800000',
            'gzip_bytes=$(gzip -9 -c _site/index.html',
            'gzip_bytes > 450000',
            'total_bytes > 2000000',
            'from smoke_test_site import snapshot_checksum, validate_html',
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
            '--retries 12',
        ):
            self.assertIn(required, deploy_job)
        self.assertIn('contents: read', deploy_job)
        self.assertIn('persist-credentials: false', deploy_job)

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

    def test_refresh_rolls_back_promoted_snapshot_before_any_git_staging(self):
        for required in (
            'PROMOTED_OUTPUTS=(',
            '"$WORK_DIR/$output.previous.json"',
            '"$WORK_DIR/$output.previous-missing"',
            'if ! "$PYTHON" -m unittest -q; then',
            'Regression suite failed; restoring the previous local snapshot.',
            'mv "$WORK_DIR/$output.previous.json" "$ROOT/$output"',
            'rm -f "$ROOT/$output"',
        ):
            self.assertIn(required, self.refresh)

        validate_at = self.refresh.index('"$PYTHON" validate_pipeline.py')
        backup_at = self.refresh.index('PROMOTED_OUTPUTS=(')
        promote_at = self.refresh.index(
            'mv "$WORK_DIR/substack.candidate.json" "$ROOT/all_posts.json"'
        )
        regression_at = self.refresh.index('if ! "$PYTHON" -m unittest -q; then')
        git_stage_at = self.refresh.index('git add -- "${TRACKED_OUTPUTS[@]}"')
        self.assertLess(validate_at, backup_at)
        self.assertLess(backup_at, promote_at)
        self.assertLess(promote_at, regression_at)
        self.assertLess(regression_at, git_stage_at)

    def test_transaction_backups_are_removed_by_cleanup(self):
        cleanup = self.refresh.split('cleanup() {', 1)[1].split('\n}', 1)[0]
        self.assertIn('rm -f "$WORK_DIR"/*.json', cleanup)
        self.assertIn('rm -f "$WORK_DIR"/*.previous-missing', cleanup)


if __name__ == '__main__':
    unittest.main()
