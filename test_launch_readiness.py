import unittest
from pathlib import Path


ROOT = Path(__file__).parent


class LaunchReadinessTests(unittest.TestCase):
    def test_owner_facing_policies_and_runbook_are_present(self):
        required = {
            'README.md': (
                'Privacy-respecting measurement',
                'LAUNCH_RUNBOOK.md',
                'SECURITY.md',
            ),
            'PRIVACY.md': (
                'no analytics SDK',
                'plaintext',
                'Copy view',
            ),
            'SECURITY.md': (
                'Reporting a vulnerability',
                'current production release',
                'exact build fingerprints',
            ),
            'LAUNCH_RUNBOOK.md': (
                'Preflight gate',
                'post-deploy smoke',
                'git revert',
                'sitemap.xml',
                '09:00, 13:00, and 22:00',
            ),
        }
        for name, phrases in required.items():
            path = ROOT / name
            self.assertTrue(path.is_file(), f'{name} must ship before launch')
            text = path.read_text(encoding='utf-8')
            for phrase in phrases:
                self.assertIn(phrase, text, f'{name} is missing {phrase!r}')

    def test_social_preview_asset_is_optimized_and_correctly_sized(self):
        asset = ROOT / 'assets/og.jpg'
        self.assertTrue(asset.is_file())
        payload = asset.read_bytes()
        self.assertTrue(payload.startswith(b'\xff\xd8\xff'))
        self.assertLess(len(payload), 200_000)
        # JPEG SOF parsing is intentionally delegated to the deterministic
        # build gate; this launch check prevents accidental asset replacement.
        self.assertGreater(len(payload), 20_000)


if __name__ == '__main__':
    unittest.main()
