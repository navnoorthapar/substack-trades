#!/usr/bin/env python3
"""Fail closed when a refresh would publish corrupt or catastrophically small data."""
import argparse
import json
import re
import sys
from pathlib import Path


VALID_DIRECTIONS = {
    'long', 'short', 'long/short', 'arbitrage/relative value', 'unspecified',
}
VALID_INSTRUMENTS = {
    'equity', 'volatility', 'option', 'bond', 'futures', 'commodity', 'FX',
    'repo', 'swap', 'CDS', 'prediction_market', 'weather_derivative',
    'unspecified',
}
DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}')


def load_list(path, label):
    try:
        with open(path, encoding='utf-8') as handle:
            value = json.load(handle)
    except Exception as exc:
        raise ValueError(f'{label} is not valid JSON: {exc}') from exc
    if not isinstance(value, list):
        raise ValueError(f'{label} must contain a JSON list')
    return value


def require(condition, message):
    if not condition:
        raise ValueError(message)


def validate_posts(posts):
    require(posts, 'post snapshot is empty')
    urls = []
    slugs = []
    for index, post in enumerate(posts):
        require(isinstance(post, dict), f'post {index} is not an object')
        url = post.get('url')
        slug = post.get('slug')
        date = post.get('post_date') or ''
        require(isinstance(url, str) and url.startswith('https://navnoorbawa.substack.com/p/'),
                f'post {index} has an invalid URL')
        require(isinstance(slug, str) and slug, f'post {index} has no slug')
        require(DATE_RE.match(date), f'post {index} has an invalid date')
        urls.append(url)
        slugs.append(slug)
    require(len(urls) == len(set(urls)), 'post snapshot contains duplicate URLs')
    require(len(slugs) == len(set(slugs)), 'post snapshot contains duplicate slugs')
    return set(urls)


def validate_article_index(articles, post_urls):
    require(articles, 'article index is empty')
    urls = []
    for index, article in enumerate(articles):
        require(isinstance(article, dict), f'article {index} is not an object')
        url = article.get('url')
        date = article.get('post_date') or ''
        require(isinstance(url, str) and url in post_urls,
                f'article {index} is not present in the fetched post snapshot')
        require(DATE_RE.match(date), f'article {index} has an invalid date')
        urls.append(url)
    require(len(urls) == len(set(urls)), 'article index contains duplicate URLs')
    require(set(urls) == post_urls, 'article index does not exactly match the fetched posts')
    return set(urls)


def validate_trades(trades, article_urls):
    require(trades, 'trade output is empty')
    represented_articles = set()
    seen = set()
    required = ('article_title', 'article_url', 'article_date', 'trade_description',
                'instruments', 'direction')
    for index, trade in enumerate(trades):
        require(isinstance(trade, dict), f'trade {index} is not an object')
        missing = [key for key in required if key not in trade]
        require(not missing, f'trade {index} is missing fields: {", ".join(missing)}')
        url = trade.get('article_url')
        date = trade.get('article_date') or ''
        desc = trade.get('trade_description') or ''
        instruments = trade.get('instruments')
        direction = trade.get('direction') or 'unspecified'
        require(url in article_urls, f'trade {index} points to an unknown article')
        require(DATE_RE.match(date), f'trade {index} has an invalid article date')
        require(isinstance(desc, str) and len(desc.strip()) >= 20,
                f'trade {index} has an empty/short description')
        require(isinstance(instruments, list) and instruments,
                f'trade {index} has no instrument list')
        unknown_instruments = set(instruments) - VALID_INSTRUMENTS
        require(not unknown_instruments,
                f'trade {index} has invalid instruments: {sorted(unknown_instruments)}')
        require(direction in VALID_DIRECTIONS,
                f'trade {index} has invalid direction: {direction!r}')
        duplicate_key = (url, desc[:150])
        require(duplicate_key not in seen, f'trade {index} duplicates an earlier trade')
        seen.add(duplicate_key)
        represented_articles.add(url)
    return represented_articles


def validate_regression(trades, represented_articles, previous_path, minimum_ratio):
    if not previous_path or not previous_path.exists():
        return
    previous = load_list(previous_path, 'previous trade output')
    if not previous:
        return
    previous_articles = {trade.get('article_url') for trade in previous if trade.get('article_url')}
    minimum_trades = max(1, int(len(previous) * minimum_ratio))
    minimum_articles = max(1, int(len(previous_articles) * minimum_ratio))
    require(len(trades) >= minimum_trades,
            f'trade count collapsed from {len(previous)} to {len(trades)} '
            f'(minimum allowed: {minimum_trades})')
    require(len(represented_articles) >= minimum_articles,
            f'trade-bearing article count collapsed from {len(previous_articles)} to '
            f'{len(represented_articles)} (minimum allowed: {minimum_articles})')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--posts', type=Path, required=True)
    parser.add_argument('--articles', type=Path, required=True)
    parser.add_argument('--trades', type=Path, required=True)
    parser.add_argument('--previous-trades', type=Path)
    parser.add_argument('--minimum-ratio', type=float, default=0.5)
    args = parser.parse_args()

    try:
        require(0 < args.minimum_ratio <= 1, 'minimum ratio must be in (0, 1]')
        posts = load_list(args.posts, 'post snapshot')
        articles = load_list(args.articles, 'article index')
        trades = load_list(args.trades, 'trade output')
        post_urls = validate_posts(posts)
        article_urls = validate_article_index(articles, post_urls)
        represented_articles = validate_trades(trades, article_urls)
        validate_regression(trades, represented_articles, args.previous_trades,
                            args.minimum_ratio)
    except ValueError as exc:
        print(f'VALIDATION FAILED: {exc}', file=sys.stderr)
        return 1

    newest = max((article.get('post_date') or '')[:10] for article in articles)
    print(f'Validation passed: {len(articles)} articles, {len(trades)} trades, '
          f'{len(represented_articles)} trade-bearing articles, newest {newest}.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
