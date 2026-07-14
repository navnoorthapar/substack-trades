import json
import re
import unittest
from pathlib import Path

from article_briefs import (
    build_article_brief,
    classify_heading,
    validate_brief_against_body,
    validate_brief_structure,
)


class ArticleBriefTests(unittest.TestCase):
    def sample_post(self):
        body = """The paper's central claim uses 24.6% and $5.5B in context, but does not call either figure a recommendation.

The mechanism
The hedge ratio changes because the volatility term is state dependent. D.E. Shaw is a name, not a sentence boundary.

What the numbers say
The study reports a 24.6% gain for calls, 19.7% for puts, and an RMSE of 1.0% across 2,500 observations.

The strongest objection
The result uses one historical window and excludes transaction costs, so it does not establish a live return.

What would change this view
The view fails if the effect disappears out of sample or costs exceed the measured spread.

What to watch
The committee's July 16, 2026 amendment deadline is the first public checkpoint. The December 1, 2026 vote is the second.

Sources
https://example.com/reference

→ Join the Patreon community here"""
        return {
            'body_text': body,
            'post_date': '2026-07-11T10:00:00Z',
        }

    def test_builds_exact_authored_spans_in_source_order(self):
        post = self.sample_post()
        brief = build_article_brief(post)
        validate_brief_against_body(brief, post['body_text'])
        self.assertEqual(
            [section['kind'] for section in brief['sections']],
            ['mechanism', 'evidence', 'countercase', 'falsifier', 'implementation'],
        )
        self.assertIn('24.6%', brief['sections'][1]['text'])
        self.assertIn('19.7%', brief['sections'][1]['text'])
        self.assertIn('RMSE of 1.0%', brief['sections'][1]['text'])
        self.assertNotIn('Patreon', str(brief))

    def test_checkpoints_are_exact_unique_and_chronological(self):
        post = self.sample_post()
        brief = build_article_brief(post)
        self.assertEqual(
            [checkpoint['date'] for checkpoint in brief['checkpoints']],
            ['2026-07-16', '2026-12-01'],
        )
        self.assertTrue(all(
            checkpoint['context_kind'] == 'implementation'
            for checkpoint in brief['checkpoints']
        ))

    def test_corrupted_span_fails_closed(self):
        post = self.sample_post()
        brief = build_article_brief(post)
        brief['lead']['text'] = 'A generated conclusion.'
        with self.assertRaisesRegex(ValueError, 'span metadata|hash|exact source span'):
            validate_brief_against_body(brief, post['body_text'])

    def test_schema_rejects_duplicate_section_kinds(self):
        brief = build_article_brief(self.sample_post())
        brief['sections'][1] = dict(brief['sections'][0])
        with self.assertRaisesRegex(ValueError, 'duplicated'):
            validate_brief_structure(brief)

    def test_heading_classifier_abstains_on_promotion_and_references(self):
        self.assertEqual(classify_heading('What Would Change This View'), 'falsifier')
        self.assertEqual(classify_heading('The Actionable Implication'), 'implementation')
        self.assertIsNone(classify_heading('Sources & References'))
        self.assertIsNone(classify_heading('→ Join the Patreon community here'))
        self.assertIsNone(classify_heading('The risk infrastructure that worked'))
        self.assertIsNone(classify_heading('Risk Factor Decomposition Analysis'))
        self.assertIsNone(classify_heading('Risk-adjusted returns'))
        self.assertIsNone(classify_heading('Financial engineering: control without ownership'))
        self.assertIsNone(classify_heading('Why This Matters'))

    def test_lead_skips_promotional_and_byline_boilerplate(self):
        post = {
            'title': 'A Source-Led Research Note',
            'post_date': '2026-07-11T10:00:00Z',
            'body_text': """Unlock the exclusive trade execution details and full institutional analysis by supporting this work on Patreon.

By Navnoor Bawa · YouTube: The Mathematical Trader

Disclaimer: This project is purely educational. No part of this article constitutes financial or investment advice.

Part of my systematic trading research series exploring alternative data and quantitative strategies

This article is dedicated to my maternal grandfather, whose wisdom and love have shaped my journey.

The paper tests 2,500 observations and reports a 24.6% historical spread, while explicitly withholding any live-return or portfolio-suitability claim.""",
        }
        brief = build_article_brief(post)
        self.assertIsNotNone(brief['lead'])
        self.assertTrue(brief['lead']['text'].startswith('The paper tests'))
        self.assertNotRegex(str(brief), r'(?i)patreon|youtube|supporting this work')

    def test_promotional_playbook_and_article_title_are_not_sections(self):
        title = (
            "Trend-Following Didn’t Get Crowded. It Hit a Capacity Ceiling "
            "Near $4 Billion, and the SG Trend Index Sits Entirely Above It"
        )
        post = {
            'title': title,
            'post_date': '2026-07-11T10:00:00Z',
            'body_text': f"""{title}

The study compares the ten largest programs with two independent capacity estimates and does not infer a live recommendation from either estimate.

▶ Watch the video here — The Full Hedge Fund Playbook
📌 The institutional-grade trade note companion and live Catalyst Watch table are published on Patreon for subscribers.""",
        }
        brief = build_article_brief(post)
        headings = [section['heading'] for section in brief['sections']]
        self.assertNotIn(title, headings)
        self.assertFalse(any(re.search(
            r'(?i)patreon|subscriber|watch the video|companion',
            section['heading'] + ' ' + section['text'],
        ) for section in brief['sections']))

    def test_prose_sentence_cannot_be_split_into_heading_and_passage(self):
        post = {
            'title': 'Tail-Risk Replication',
            'post_date': '2026-07-11T10:00:00Z',
            'body_text': """The replication separates an historical result from the mechanism that might preserve it after publication.

The Kelly-Jiang result earned its reputation. Applying the Hill formula
ξ̂ = (1/k) ∑ log(Xᵢ / Xₖ₊₁)
to the cross-section of firm-level daily crashes extracts a time-varying common tail factor, but this continuation is not an authored section.

What the numbers say
The replication reports a t-statistic of 0.57 across the complete sample and contrasts it with the originally published 2.15 estimate.""",
        }
        brief = build_article_brief(post)
        evidence = [
            section for section in brief['sections']
            if section['kind'] == 'evidence'
        ]
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]['heading'], 'What the numbers say')
        self.assertTrue(evidence[0]['text'].startswith('The replication reports'))

    def test_intervening_unclassified_subheading_prevents_attachment(self):
        post = {
            'title': 'Calendar Test',
            'post_date': '2026-07-11T10:00:00Z',
            'body_text': """The note distinguishes explicit public catalysts from nearby prose that belongs to another subsection.

What to watch
Quarterly releases
The August 1, 2026 vote is a public checkpoint, but it belongs to the unclassified Quarterly releases subsection rather than directly to What to watch.""",
        }
        brief = build_article_brief(post)
        self.assertFalse(any(
            section['kind'] == 'implementation'
            for section in brief['sections']
        ))
        self.assertEqual(brief['checkpoints'], [])

    def test_reference_section_is_terminal_for_section_detection(self):
        post = {
            'title': 'Reference Boundary Test',
            'post_date': '2026-07-11T10:00:00Z',
            'body_text': """The article contains a substantive lead but no authored evidence section in its analytical body.

Sources
https://example.com/research-results
The linked paper reports a 24.6% historical return across 2,500 observations and is a citation entry, not an authored dossier section.""",
        }
        brief = build_article_brief(post)
        self.assertEqual(brief['sections'], [])

    def test_checkpoints_require_explicit_calendar_heading_and_future_date(self):
        post = {
            'title': 'Checkpoint Precision Test',
            'post_date': '2026-07-11T10:00:00Z',
            'body_text': """The note contains dates in several contexts, only one of which is an explicit public checkpoint.

Capacity Analysis
Article updated July 11, 2026 to correct an attribution after a fact-checking review.
The July 20, 2026 earnings release affects capacity assumptions but is not presented as a watch-list catalyst.

What to watch
The committee's August 1, 2026 vote is the explicit public checkpoint for this thesis.""",
        }
        brief = build_article_brief(post)
        self.assertEqual(
            [checkpoint['date'] for checkpoint in brief['checkpoints']],
            ['2026-08-01'],
        )
        self.assertNotIn('Article updated', str(brief['checkpoints']))

    def test_tracked_catalogue_contains_no_display_boilerplate(self):
        articles = json.loads(
            (Path(__file__).parent / 'articles_index.json').read_text(
                encoding='utf-8'
            )
        )
        boilerplate = re.compile(
            r'(?i)(?:patreon|support(?:ing)? (?:this|my) work|'
            r'unlock the exclusive|watch the video|notebooklm|subscribe to|'
            r'early access|by joining|trade note companion)'
        )
        byline = re.compile(r'(?i)^\s*(?:by\s+)?navnoor bawa(?:\s*[|\xb7—-].*)?$')
        failures = []
        for article in articles:
            brief = article.get('brief') or {}
            lead = brief.get('lead') or {}
            if boilerplate.search(str(lead.get('text') or '')) or byline.fullmatch(
                str(lead.get('text') or '').strip()
            ):
                failures.append((article.get('source_id'), 'lead'))
            for section in brief.get('sections') or []:
                combined = str(section.get('heading') or '') + ' ' + str(
                    section.get('text') or ''
                )
                if (boilerplate.search(combined)
                        or re.search(r'https?://', str(section.get('heading') or ''))
                        or str(section.get('heading') or '').strip().casefold()
                        == str(article.get('title') or '').strip().casefold()):
                    failures.append((article.get('source_id'), section.get('kind')))
            for checkpoint in brief.get('checkpoints') or []:
                if re.match(r'(?i)^\s*(?:article|last) updated\b', str(
                    checkpoint.get('text') or ''
                )):
                    failures.append((article.get('source_id'), 'checkpoint'))
        self.assertEqual(failures, [])


if __name__ == '__main__':
    unittest.main()
