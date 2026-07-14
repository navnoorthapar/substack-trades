import re
import unittest
from pathlib import Path


ROOT = Path(__file__).parent


class DeploymentConfigurationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = (ROOT / '.github/workflows/update.yml').read_text(encoding='utf-8')
        cls.refresh = (ROOT / 'refresh.sh').read_text(encoding='utf-8')
        cls.ignore = (ROOT / '.gitignore').read_text(encoding='utf-8').splitlines()

    def test_workflow_has_quality_gates_and_current_official_pages_actions(self):
        for required in (
            'actions/checkout@v6',
            'actions/setup-python@v6',
            'python -m unittest',
            'validate_pipeline.py',
            'SITE_OUTPUT_DIR:',
            'actions/configure-pages@v6',
            'actions/upload-pages-artifact@v5',
            'actions/deploy-pages@v5',
        ):
            self.assertIn(required, self.workflow)

    def test_pull_requests_cannot_deploy(self):
        self.assertRegex(self.workflow, r'(?m)^\s*push:')
        self.assertRegex(self.workflow, r'(?m)^\s*pull_request:')
        self.assertNotRegex(self.workflow, r'(?m)^\s*paths:')
        self.assertGreaterEqual(self.workflow.count("github.event_name != 'pull_request'"), 3)
        self.assertIn("github.ref == 'refs/heads/main'", self.workflow)

    def test_permissions_are_least_privilege_and_checkout_cannot_push(self):
        self.assertRegex(self.workflow, r'(?m)^permissions: \{\}$')
        self.assertIn('persist-credentials: false', self.workflow)
        self.assertIn('contents: read', self.workflow)
        self.assertIn('pages: write', self.workflow)
        self.assertIn('id-token: write', self.workflow)
        self.assertNotIn('contents: write', self.workflow)
        self.assertNotRegex(self.workflow, r'(?m)^\s*run:\s*git (?:commit|push)')

    def test_generated_site_is_untracked_and_refresh_commits_only_source_data(self):
        self.assertIn('/docs/', self.ignore)
        self.assertIn('/_site/', self.ignore)
        self.assertNotIn('build_site.py', self.refresh)
        self.assertNotIn('git add docs', self.refresh)
        self.assertIn('python', self.refresh)
        self.assertIn('-m unittest', self.refresh)
        self.assertIn('TRACKED_OUTPUTS=(', self.refresh)
        self.assertIn('git add -- "${TRACKED_OUTPUTS[@]}"', self.refresh)
        self.assertIn('git diff --staged --quiet -- "${TRACKED_OUTPUTS[@]}"', self.refresh)
        self.assertRegex(
            self.refresh,
            r'git commit --only[\s\S]*-- "\$\{TRACKED_OUTPUTS\[@\]\}"',
        )

    def test_production_deployments_are_serialized_without_cancellation(self):
        self.assertIn("|| 'production'", self.workflow)
        self.assertIn("cancel-in-progress: ${{ github.event_name == 'pull_request' }}", self.workflow)


if __name__ == '__main__':
    unittest.main()
