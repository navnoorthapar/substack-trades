import copy
import dataclasses
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import validate_release


ROOT = Path(__file__).parent
REVISION = 'release-validator-test'
RELEASE_CLI_TIMEOUT_SECONDS = 180


def can_flip_publication_access(article):
    """Report whether flipping publication_access keeps the row wire-valid.

    A member row may only become public when it carries no preview text, and
    a public row may only become member while it stays excerpt-only.
    """
    if article['publication_access'] == 'public':
        return article['content_status'] == 'excerpt'
    if article['publication_access'] == 'member':
        return article['member_preview_chars'] == 0
    return False


def can_promote_to_full(article):
    """Report whether promoting content_status to full keeps the row valid.

    Full content requires non-member access and a timestamp-bound current
    body revision.
    """
    return (
        article['content_status'] == 'excerpt'
        and article['publication_access'] != 'member'
        and article['body_revision_status'] == 'current'
        and bool(article['source_updated_at'])
        and article['source_updated_at']
        == article['observed_source_updated_at']
    )


def flip_publication_access(article):
    article['publication_access'] = (
        'member' if article['publication_access'] == 'public' else 'public'
    )


def promote_to_full(article):
    article['content_status'] = 'full'


class ReleaseValidatorTests(unittest.TestCase):
    case: tempfile.TemporaryDirectory
    base: Path
    site: Path
    articles: Path
    trades: Path
    manifest: Path

    @classmethod
    def setUpClass(cls):
        cls.case = tempfile.TemporaryDirectory(prefix='nrt-release-validator-')
        cls.base = Path(cls.case.name)
        source_root = cls.base / 'source'
        source_root.mkdir()
        for source in ROOT.glob('*.py'):
            shutil.copy2(source, source_root / source.name)
        for name in (
            'articles_index.json',
            'trades_extracted.json',
            'treasury_curve.json',
            'snapshot_manifest.json',
        ):
            shutil.copy2(ROOT / name, source_root / name)
        shutil.copytree(ROOT / 'assets', source_root / 'assets')

        fixture_manifest = source_root / 'snapshot_manifest.json'
        manifest = json.loads(fixture_manifest.read_text(encoding='utf-8'))
        manifest['checked_at'] = (
            datetime.now(timezone.utc)
            .isoformat(timespec='seconds')
            .replace('+00:00', 'Z')
        )
        fixture_manifest.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + '\n',
            encoding='utf-8',
        )

        cls.site = cls.base / 'site'
        environment = os.environ.copy()
        environment.update({
            'SITE_OUTPUT_DIR': str(cls.site),
            'SITE_REVISION': REVISION,
        })
        subprocess.run(
            [sys.executable, str(source_root / 'build_site.py')],
            cwd=source_root,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
        cls.articles = source_root / 'articles_index.json'
        cls.trades = source_root / 'trades_extracted.json'
        cls.manifest = fixture_manifest

    @classmethod
    def tearDownClass(cls):
        cls.case.cleanup()

    @contextmanager
    def cloned_site(self):
        with tempfile.TemporaryDirectory(
                prefix='nrt-release-mutation-') as directory:
            target = Path(directory) / 'site'
            try:
                shutil.copytree(
                    self.site,
                    target,
                    copy_function=os.link,
                )
            except OSError:
                if target.exists():
                    shutil.rmtree(target)
                shutil.copytree(self.site, target)
            yield target

    @staticmethod
    def rewrite(path, payload):
        path.unlink()
        path.write_bytes(payload)

    @staticmethod
    def embedded_articles(site):
        payload = json.loads(
            (site / 'article_catalog.json').read_text(encoding='utf-8')
        )
        return payload['articles']

    def replace_embedded_articles(self, site, transform):
        path = site / 'article_catalog.json'
        payload = json.loads(path.read_text(encoding='utf-8'))
        transform(payload['articles'])
        self.rewrite(
            path,
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(',', ':'),
            ).encode('utf-8'),
        )
        self.replace_asset_digest(site, 'article_catalog.json')

    def replace_asset_digest(self, site, asset_name):
        meta_names = {
            'article_catalog.json': 'nrt-article-catalog-sha256',
            'article_briefs.json': 'nrt-brief-archive-sha256',
            'observations.json': 'nrt-observation-archive-sha256',
        }
        digest = hashlib.sha256((site / asset_name).read_bytes()).hexdigest()
        path = site / 'index.html'
        html = path.read_text(encoding='utf-8')
        pattern = re.compile(
            rf'(<meta name="{re.escape(meta_names[asset_name])}" content=")'
            r'[0-9a-f]{64}(">)',
        )
        html, count = pattern.subn(rf'\g<1>{digest}\g<2>', html)
        self.assertEqual(count, 1)
        self.rewrite(path, html.encode('utf-8'))

    def replace_observations(self, site, transform):
        path = site / 'observations.json'
        payload = json.loads(path.read_text(encoding='utf-8'))
        transform(payload['observations'])
        self.rewrite(
            path,
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(',', ':'),
            ).encode('utf-8'),
        )
        self.replace_asset_digest(site, 'observations.json')

    def validate(self, site=None, **kwargs):
        return validate_release.validate_release(
            site or self.site,
            self.articles,
            self.trades,
            self.manifest,
            kwargs.pop('expected_revision', REVISION),
            **kwargs,
        )

    def test_exact_build_passes_and_cli_emits_six_fingerprints(self):
        wire = self.embedded_articles(self.site)
        self.assertTrue(
            any('idea_ids' not in article for article in wire),
            'fixture does not exercise compact empty-list omission',
        )
        fingerprints = self.validate()
        self.assertEqual(
            tuple(fingerprints),
            validate_release.FINGERPRINT_KEYS,
        )
        self.assertTrue(all(
            re.fullmatch(r'[0-9a-f]{64}', value)
            for value in fingerprints.values()
        ))

        output = self.base / 'github-output.txt'
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / 'validate_release.py'),
                '--site', str(self.site),
                '--articles', str(self.articles),
                '--trades', str(self.trades),
                '--manifest', str(self.manifest),
                '--expected-revision', REVISION,
                '--github-output', str(output),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            # The scheduled refresh runs the complete launch suite on the
            # publication Mac. Keep the CLI proof bounded while allowing for
            # cold filesystem caches and transient CPU contention from that
            # surrounding release gate.
            timeout=RELEASE_CLI_TIMEOUT_SECONDS,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        output_rows = dict(
            line.split('=', 1)
            for line in output.read_text(encoding='utf-8').splitlines()
        )
        self.assertEqual(output_rows, fingerprints)

    def test_wrong_revision_and_invalid_inline_javascript_fail_closed(self):
        with self.assertRaisesRegex(ValueError, 'revision'):
            self.validate(expected_revision='wrong-revision')

        with self.cloned_site() as site:
            path = site / 'index.html'
            html = path.read_text(encoding='utf-8')
            html = html.replace(
                '</body>',
                '<script>const releaseSyntaxFailure = ;</script></body>',
                1,
            )
            self.rewrite(path, html.encode('utf-8'))
            with self.assertRaisesRegex(ValueError, 'JavaScript syntax'):
                self.validate(site)

    def test_article_wire_schema_version_is_unique_and_current(self):
        declaration = (
            'const ARTICLE_WIRE_SCHEMA_VERSION = '
            f'{validate_release.ARTICLE_WIRE_SCHEMA_VERSION};'
        )
        with self.cloned_site() as site:
            path = site / 'index.html'
            html = path.read_text(encoding='utf-8')
            self.assertEqual(html.count(declaration), 1)
            html = html.replace(
                declaration,
                'const ARTICLE_WIRE_SCHEMA_VERSION = 999999;',
                1,
            )
            self.rewrite(path, html.encode('utf-8'))
            with self.assertRaisesRegex(ValueError, 'current client contract'):
                self.validate(site)

        with self.cloned_site() as site:
            path = site / 'index.html'
            html = path.read_text(encoding='utf-8')
            html = html.replace(declaration, f'{declaration}\n{declaration}', 1)
            self.rewrite(path, html.encode('utf-8'))
            with self.assertRaisesRegex(ValueError, 'exactly one'):
                self.validate(site)

    def test_allowlist_symlink_and_png_corruption_fail_closed(self):
        with self.cloned_site() as site:
            (site / 'unexpected.txt').write_text('unexpected', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'allowlist'):
                self.validate(site)

        with self.cloned_site() as site:
            target = site / 'robots.txt'
            target.unlink()
            target.symlink_to('sitemap.xml')
            with self.assertRaisesRegex(ValueError, 'symbolic link'):
                self.validate(site)

        with self.cloned_site() as site:
            card = next((site / 'cards').iterdir())
            payload = bytearray(card.read_bytes())
            payload[0] ^= 0xFF
            self.rewrite(card, bytes(payload))
            with self.assertRaisesRegex(ValueError, 'indexed-PNG'):
                self.validate(site)

    def test_support_assets_require_exact_source_renderings(self):
        def mutate_robots(payload):
            expected = b'Allow: /'
            self.assertEqual(payload.count(expected), 1)
            return payload.replace(expected, b'Disallow: /', 1)

        def mutate_sitemap(payload):
            start = payload.rfind(b'<lastmod>')
            end = payload.find(b'</lastmod>', start)
            self.assertGreaterEqual(start, 0)
            self.assertGreater(end, start)
            return (
                payload[:start]
                + b'<lastmod>1900-01-01</lastmod>'
                + payload[end + len(b'</lastmod>'):]
            )

        def mutate_manifest(payload):
            expected = b'"theme_color": "#f5f3ee"'
            self.assertEqual(payload.count(expected), 1)
            return payload.replace(
                expected,
                b'"theme_color": "#050607"',
                1,
            )

        def mutate_favicon(payload):
            expected = b'stroke="#e3ca8a"'
            self.assertEqual(payload.count(expected), 1)
            return payload.replace(expected, b'stroke="#e3ca89"', 1)

        for asset_name, transform in (
            ('robots.txt', mutate_robots),
            ('sitemap.xml', mutate_sitemap),
            ('site.webmanifest', mutate_manifest),
            ('favicon.svg', mutate_favicon),
        ):
            with self.subTest(asset=asset_name):
                with self.cloned_site() as site:
                    path = site / asset_name
                    self.rewrite(path, transform(path.read_bytes()))
                    with self.assertRaisesRegex(
                            ValueError,
                            rf'{re.escape(asset_name)} differs'):
                        self.validate(site)

    def test_og_image_requires_exact_bytes_and_dimensions(self):
        with self.cloned_site() as site:
            path = site / validate_release.SOCIAL_IMAGE_NAME
            payload = bytearray(path.read_bytes())
            payload[-3] ^= 0x01
            self.rewrite(path, bytes(payload))
            with self.assertRaisesRegex(
                    ValueError, 'differs from tracked assets/og.jpg'):
                self.validate(site)

        with self.cloned_site() as site:
            path = site / validate_release.SOCIAL_IMAGE_NAME
            payload = bytearray(path.read_bytes())
            dimensions = (
                (630).to_bytes(2, 'big')
                + (1200).to_bytes(2, 'big')
            )
            offset = payload.find(dimensions)
            self.assertGreaterEqual(offset, 0)
            payload[offset + 2:offset + 4] = (1199).to_bytes(2, 'big')
            self.assertEqual(
                validate_release._jpeg_dimensions(bytes(payload)),
                (1199, 630),
            )
            self.rewrite(path, bytes(payload))
            with self.assertRaisesRegex(ValueError, 'exactly 1200x630'):
                self.validate(site)

    def test_non_header_share_asset_mutations_fail_exact_rendering(self):
        with self.cloned_site() as site:
            cards = sorted((site / 'cards').iterdir())
            card = cards[len(cards) // 2]
            payload = bytearray(card.read_bytes())
            payload[len(payload) // 2] ^= 0x01
            self.rewrite(card, bytes(payload))
            with self.assertRaisesRegex(ValueError, 'share card differs'):
                self.validate(site)

        with self.cloned_site() as site:
            stubs = sorted((site / 'a').iterdir())
            stub = stubs[len(stubs) // 2]
            self.rewrite(stub, stub.read_bytes() + b'<!-- tampered -->')
            with self.assertRaisesRegex(ValueError, 'article stub differs'):
                self.validate(site)

    def test_production_size_policy_cannot_be_weakened_by_cli_flags(self):
        policy = validate_release.PRODUCTION_POLICY
        self.assertEqual(policy.index_max_bytes, 900_000)
        self.assertEqual(policy.index_gzip_max_bytes, 250_000)
        self.assertEqual(policy.article_catalog_max_bytes, 4_000_000)
        self.assertEqual(policy.brief_max_bytes, 800_000)
        self.assertEqual(policy.observation_max_bytes, 1_500_000)
        self.assertEqual(policy.total_max_bytes, 20_000_000)
        parser = validate_release._build_parser()
        option_strings = {
            option
            for action in parser._actions
            for option in action.option_strings
        }
        self.assertFalse(any('max' in option for option in option_strings))

        too_small = dataclasses.replace(
            policy,
            index_max_bytes=(self.site / 'index.html').stat().st_size - 1,
        )
        with self.assertRaisesRegex(ValueError, 'index.html size'):
            self.validate(policy=too_small)

    def test_embedded_article_set_requires_exact_source_id_url_bijection(self):
        with self.cloned_site() as site:
            self.replace_embedded_articles(site, lambda rows: rows.pop())
            with self.assertRaisesRegex(ValueError, 'catalogue count'):
                self.validate(site)

        with self.cloned_site() as site:
            def duplicate_identity(rows):
                rows[1]['id'] = rows[0]['id']
                rows[1]['url'] = rows[0]['url']

            self.replace_embedded_articles(site, duplicate_identity)
            with self.assertRaisesRegex(ValueError, 'duplicated'):
                self.validate(site)

    def test_embedded_article_metadata_requires_exact_source_projection(self):
        # Every mutation below must leave its row valid against the client
        # wire contract. A row that violates the contract is rejected during
        # hydration, before the source/build projection is ever compared, so
        # the assertion would pass on the wrong error. Selecting a row by
        # position is not safe either: the payload is ordered by publication
        # date, so one new post can change which row a bare next() picks.
        def select(rows, predicate, requirement):
            row = next(
                (candidate for candidate in rows if predicate(candidate)),
                None,
            )
            self.assertIsNotNone(
                row,
                f'the built payload has no article that is {requirement}, '
                'so this projection check cannot be exercised',
            )
            return row

        def mutate_title(rows):
            rows[0]['title'] += ' [tampered]'

        def mutate_content_status(rows):
            row = select(
                rows,
                can_promote_to_full,
                'a non-member excerpt with a current body revision',
            )
            promote_to_full(row)

        def mutate_wordcount(rows):
            rows[0]['wordcount'] += 220

        def mutate_body_revision(rows):
            row = select(
                rows,
                lambda candidate: (
                    candidate['body_revision_status'] == 'current'
                ),
                'a current body revision',
            )
            row['body_revision_status'] = 'unverified'
            row['content_status'] = 'excerpt'

        def mutate_body_revision_timestamps(rows):
            row = select(
                rows,
                lambda candidate: (
                    candidate['body_revision_status'] == 'current'
                ),
                'a current body revision',
            )
            row['source_updated_at'] = '2001-01-01T00:00:00Z'
            row['observed_source_updated_at'] = '2001-01-01T00:00:00Z'

        def mutate_publication_access(rows):
            row = select(
                rows,
                can_flip_publication_access,
                'able to flip publication_access and stay wire-valid',
            )
            flip_publication_access(row)

        def mutate_member_preview_chars(rows):
            row = select(
                rows,
                lambda candidate: (
                    candidate['publication_access'] == 'member'
                ),
                'a member-access article',
            )
            row['member_preview_chars'] = (
                1 if row['member_preview_chars'] == 0 else 0
            )

        for field, transform in (
            ('title', mutate_title),
            ('content_status', mutate_content_status),
            ('wordcount', mutate_wordcount),
            ('body_revision_status', mutate_body_revision),
            ('source_updated_at', mutate_body_revision_timestamps),
            ('publication_access', mutate_publication_access),
            ('member_preview_chars', mutate_member_preview_chars),
        ):
            with self.subTest(field=field):
                with self.cloned_site() as site:
                    self.replace_embedded_articles(site, transform)
                    with self.assertRaisesRegex(
                            ValueError,
                            rf'source/build projection.*{field}'):
                        self.validate(site)

    def test_projection_mutations_survive_any_payload_order(self):
        """Keep the projection checks independent of publication order.

        The embedded payload is ordered by publication date, so a single new
        post can change which row the projection mutations select. If a
        selected row violates the client wire contract once mutated, hydration
        rejects the payload before the source/build projection is ever
        compared, and the assertion passes for the wrong reason. That failure
        mode is invisible to CI until fresh data reorders the payload, at
        which point it blocks the publisher instead.
        """
        rows = self.embedded_articles(self.site)
        self.assertGreater(len(rows), 1)

        for field, is_eligible, mutate in (
            (
                'publication_access',
                can_flip_publication_access,
                flip_publication_access,
            ),
            ('content_status', can_promote_to_full, promote_to_full),
        ):
            with self.subTest(field=field):
                eligible = [row for row in rows if is_eligible(row)]
                self.assertTrue(
                    eligible,
                    f'no embedded article can exercise the {field} '
                    'projection check',
                )
                for row in eligible:
                    candidate = copy.deepcopy(row)
                    mutate(candidate)
                    try:
                        validate_release.hydrate_client_article(candidate)
                    except ValueError as exc:
                        self.fail(
                            f'mutating {field} on article {row["id"]} breaks '
                            'the client wire contract, so the projection '
                            f'check cannot run: {exc}',
                        )

    def test_article_order_aggregates_and_masks_require_exact_projection(self):
        def reverse_order(rows):
            rows.reverse()

        def mutate_directions(rows):
            row = next(candidate for candidate in rows
                       if candidate.get('directions'))
            row['directions'].append('tampered-direction')

        def mutate_brief_mask(rows):
            row = next(candidate for candidate in rows if '_b' in candidate)
            row['_b'][0] ^= 1

        def mutate_coverage_mask(rows):
            row = next(candidate for candidate in rows if '_q' in candidate)
            row['_q'] ^= 1

        for label, transform, message in (
            ('order', reverse_order, 'chronology/order'),
            ('directions', mutate_directions, 'derived fields.*directions'),
            (
                'brief_features',
                mutate_brief_mask,
                'derived fields.*brief_features',
            ),
            ('coverage', mutate_coverage_mask, 'derived fields.*has_'),
        ):
            with self.subTest(label=label):
                with self.cloned_site() as site:
                    self.replace_embedded_articles(site, transform)
                    with self.assertRaisesRegex(ValueError, message):
                        self.validate(site)

    def test_compact_idea_ids_reject_wrong_type_and_missing_real_references(self):
        with self.cloned_site() as site:
            def invalidate_type(rows):
                row = next(
                    candidate for candidate in rows
                    if 'idea_ids' not in candidate
                )
                row['idea_ids'] = None

            self.replace_embedded_articles(site, invalidate_type)
            with self.assertRaisesRegex(ValueError, 'wire contract'):
                self.validate(site)

        with self.cloned_site() as site:
            def remove_real_references(rows):
                row = next(
                    candidate for candidate in rows
                    if candidate.get('idea_ids')
                )
                row.pop('idea_ids')

            self.replace_embedded_articles(site, remove_real_references)
            with self.assertRaisesRegex(
                    ValueError, 'derived fields.*idea_ids'):
                self.validate(site)

    def test_deferred_dossier_ownership_is_exact(self):
        with self.cloned_site() as site:
            path = site / 'article_briefs.json'
            payload = json.loads(path.read_text(encoding='utf-8'))
            payload['briefs'].pop(next(iter(payload['briefs'])))
            self.rewrite(
                path,
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(',', ':'),
                ).encode('utf-8'),
            )
            self.replace_asset_digest(site, 'article_briefs.json')
            with self.assertRaisesRegex(ValueError, 'dossier ownership'):
                self.validate(site)

    def test_inline_brief_boundary_cannot_be_moved(self):
        with self.cloned_site() as site:
            path = site / 'article_briefs.json'
            payload = json.loads(path.read_text(encoding='utf-8'))

            def move_brief_across_boundary(rows):
                inline_index = validate_release.INLINE_BRIEF_COUNT - 1
                deferred_index = validate_release.INLINE_BRIEF_COUNT
                self.assertGreater(len(rows), deferred_index)
                inline_row = rows[inline_index]
                deferred_row = rows[deferred_index]
                self.assertIn('brief', inline_row)
                self.assertNotIn('brief', deferred_row)
                inline_brief = inline_row.pop('brief')
                deferred_brief = payload['briefs'].pop(deferred_row['id'])
                deferred_row['brief'] = deferred_brief
                payload['briefs'][inline_row['id']] = inline_brief

            self.replace_embedded_articles(
                site,
                move_brief_across_boundary,
            )
            self.rewrite(
                path,
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(',', ':'),
                ).encode('utf-8'),
            )
            self.replace_asset_digest(site, 'article_briefs.json')
            with self.assertRaisesRegex(
                    ValueError, 'inline/deferred placement'):
                self.validate(site)

    def test_observation_ownership_and_content_are_lossless(self):
        with self.cloned_site() as site:
            self.replace_observations(site, lambda rows: rows.pop())
            with self.assertRaisesRegex(ValueError, 'observation count'):
                self.validate(site)

        with self.cloned_site() as site:
            path = site / 'observations.json'
            payload = json.loads(path.read_text(encoding='utf-8'))
            payload['observations'][0]['article_id'] = 'a_not_an_owner'
            self.rewrite(
                path,
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(',', ':'),
                ).encode('utf-8'),
            )
            self.replace_asset_digest(site, 'observations.json')
            with self.assertRaisesRegex(ValueError, 'ownership'):
                self.validate(site)

        with self.cloned_site() as site:
            path = site / 'observations.json'
            payload = json.loads(path.read_text(encoding='utf-8'))
            payload['observations'][0]['direction'] = 'tampered'
            self.rewrite(
                path,
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(',', ':'),
                ).encode('utf-8'),
            )
            self.replace_asset_digest(site, 'observations.json')
            with self.assertRaisesRegex(ValueError, 'content differs'):
                self.validate(site)

    def test_observation_derived_fields_and_field_set_are_exact(self):
        def mutate_manager(rows):
            rows[0]['manager'] = 'Tampered Manager'

        def mutate_documentation_score(rows):
            rows[0]['documentation_score'] += 1

        def add_unexpected_field(rows):
            rows[0]['unexpected'] = True

        for label, transform, message in (
            ('manager', mutate_manager, 'content differs'),
            (
                'documentation_score',
                mutate_documentation_score,
                'content differs',
            ),
            ('field_set', add_unexpected_field, 'wrong field set'),
        ):
            with self.subTest(label=label):
                with self.cloned_site() as site:
                    self.replace_observations(site, transform)
                    with self.assertRaisesRegex(ValueError, message):
                        self.validate(site)

    def test_observation_order_is_exact(self):
        def swap_first_two(rows):
            self.assertGreaterEqual(len(rows), 2)
            rows[0], rows[1] = rows[1], rows[0]

        with self.cloned_site() as site:
            self.replace_observations(site, swap_first_two)
            with self.assertRaisesRegex(ValueError, 'observation order'):
                self.validate(site)

    def test_data_layer_and_embedded_asset_digests_fail_closed(self):
        with self.cloned_site() as site:
            latest = site / 'data' / 'latest.json'
            self.rewrite(latest, b'{"broken":true}\n')
            with self.assertRaises(ValueError):
                self.validate(site)

        with self.cloned_site() as site:
            brief = site / 'article_briefs.json'
            payload = bytearray(brief.read_bytes())
            payload[-1] = ord(' ')
            self.rewrite(brief, bytes(payload))
            with self.assertRaisesRegex(ValueError, 'digest'):
                self.validate(site)


if __name__ == '__main__':
    unittest.main()
