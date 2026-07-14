#!/usr/bin/env python3
"""
Fetch all posts from navnoorbawa.substack.com API and save content locally.
"""
import urllib.request
import urllib.error
import json
import os
import ssl
import sys
import time
import html
import re
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).parent
POSTS_PATH = ROOT / 'all_posts.json'
ARTICLE_INDEX_PATH = ROOT / 'articles_index.json'

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/html, */*',
}


def build_ssl_context():
    """Return a verified TLS context, including on broken python.org installs.

    Some macOS Python installers point OpenSSL at a certificate bundle that no
    longer exists. The system trust bundle remains available, so prefer it when
    Python's configured CA file is missing. Verification is never disabled.
    """
    default_cafile = ssl.get_default_verify_paths().cafile
    if default_cafile and Path(default_cafile).is_file():
        return ssl.create_default_context()

    candidates = [
        os.environ.get('SSL_CERT_FILE'),
        '/etc/ssl/cert.pem',
        '/private/etc/ssl/cert.pem',
        '/etc/ssl/certs/ca-certificates.crt',
    ]
    for cafile in candidates:
        if cafile and Path(cafile).is_file():
            return ssl.create_default_context(cafile=cafile)

    # Let Python raise its normal certificate error rather than silently
    # weakening TLS if no trusted CA bundle is available.
    return ssl.create_default_context()


SSL_CONTEXT = build_ssl_context()

class MLStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self.reset()
        self.fed = []
        self.in_style = False
        self.in_script = False

    def handle_starttag(self, tag, attrs):
        if tag in ('style', 'script'):
            self.in_style = True
        # Add newlines for block elements
        if tag in ('p', 'br', 'div', 'h1', 'h2', 'h3', 'h4', 'h5', 'li', 'tr'):
            self.fed.append('\n')

    def handle_endtag(self, tag):
        if tag in ('style', 'script'):
            self.in_style = False
        if tag in ('p', 'div', 'h1', 'h2', 'h3', 'h4', 'h5', 'li', 'tr'):
            self.fed.append('\n')

    def handle_data(self, d):
        if not self.in_style:
            self.fed.append(d)

    def get_data(self):
        return ''.join(self.fed)


def strip_html(html_content):
    s = MLStripper()
    s.feed(html_content)
    text = s.get_data()
    text = html.unescape(text)
    # Clean up excessive whitespace
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r' {2,}', ' ', text)
    return text.strip()


def fetch_posts(limit=50, offset=0, attempts=3):
    url = f'https://navnoorbawa.substack.com/api/v1/posts?limit={limit}&offset={offset}'
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30, context=SSL_CONTEXT) as r:
                data = json.loads(r.read())
            if not isinstance(data, list) or not all(isinstance(post, dict) for post in data):
                raise ValueError('Substack returned an unexpected response shape')
            if any(not post.get('slug') or not post.get('post_date') for post in data):
                raise ValueError('Substack returned a post without a slug or publication date')
            return data
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(2 ** (attempt - 1))
    raise last_error


def article_metadata(post):
    """Keep the small, deployable subset needed to render every article."""
    return {
        'slug': post.get('slug', ''),
        'title': post.get('title', ''),
        'subtitle': post.get('subtitle', ''),
        'post_date': post.get('post_date', ''),
        'url': post.get('url', ''),
        'audience': post.get('audience', ''),
        'wordcount': post.get('wordcount', 0),
    }


def atomic_write_json(path, value):
    tmp_path = path.parent / (path.name + '.tmp')
    try:
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def main():
    all_posts = []
    offset = 0
    limit = 50
    failed = False
    seen_page_signatures = set()
    seen_posts = set()

    print("Fetching posts from Substack API...")

    while True:
        print(f"  Fetching offset={offset}...", end=' ', flush=True)
        try:
            posts = fetch_posts(limit=limit, offset=offset)
        except Exception as e:
            # A mid-pagination failure must be treated as fatal, NOT as
            # end-of-feed — otherwise we'd persist a truncated snapshot.
            print(f"ERROR at offset={offset}: {type(e).__name__}: {e}")
            failed = True
            break

        if not posts:
            print("No more posts.")
            break

        signature = tuple((post.get('id'), post.get('slug')) for post in posts)
        if signature in seen_page_signatures:
            print(f"ERROR at offset={offset}: Substack repeated a page; refusing a partial snapshot")
            failed = True
            break
        seen_page_signatures.add(signature)

        print(f"Got {len(posts)} posts")

        for post in posts:
            post_key = post.get('id') or post.get('slug')
            if post_key in seen_posts:
                continue
            seen_posts.add(post_key)

            body_html = post.get('body_html', '')
            body_text = strip_html(body_html) if body_html else post.get('truncated_body_text', '')

            all_posts.append({
                'slug': post.get('slug', ''),
                'title': post.get('title', ''),
                'subtitle': post.get('subtitle', ''),
                'post_date': post.get('post_date', ''),
                'url': f"https://navnoorbawa.substack.com/p/{post.get('slug', '')}",
                'audience': post.get('audience', ''),
                'meter_type': post.get('meter_type', ''),
                'type': post.get('type', ''),
                'is_published': post.get('is_published', True),
                'wordcount': post.get('wordcount', 0),
                'body_text': body_text,
                'body_html_length': len(body_html),
            })

        if len(posts) < limit:
            print("Reached end of posts.")
            break

        offset += limit
        time.sleep(0.5)  # Be polite

    print(f"\nTotal posts fetched: {len(all_posts)}")

    # ── Guard the existing snapshot ───────────────────────────────────────────
    # Never overwrite a good all_posts.json with a failed or shrunken fetch:
    # downstream extract/filter/build would regenerate from truncated data and
    # silently drop articles (including the newest ones).
    if failed:
        print("Fetch did not complete — leaving previous all_posts.json untouched.")
        sys.exit(1)

    if not all_posts:
        print("Fetch returned zero posts — leaving previous all_posts.json untouched.")
        sys.exit(1)

    if POSTS_PATH.exists():
        try:
            with open(POSTS_PATH, 'r', encoding='utf-8') as f:
                prev = json.load(f)
            prev_count = len(prev) if isinstance(prev, list) else 0
            prev_slugs = {post.get('slug') for post in prev if isinstance(post, dict) and post.get('slug')}
        except Exception:
            prev_count = 0
            prev_slugs = set()
        # A small decrease can be a legitimate deletion/unpublish. A large one
        # is much more likely to be a changed or truncated API response.
        minimum_safe_count = max(1, int(prev_count * 0.9))
        if (prev_count and len(all_posts) < minimum_safe_count
                and not os.environ.get('FORCE_FETCH')):
            print(f"Refusing to overwrite: fetched {len(all_posts)} posts, below the "
                  f"90% safety floor of {minimum_safe_count} from the previous {prev_count}. "
                  f"Set FORCE_FETCH=1 to override.")
            sys.exit(1)
        current_slugs = {post.get('slug') for post in all_posts if post.get('slug')}
        missing_slugs = prev_slugs - current_slugs
        maximum_missing = max(5, int(prev_count * 0.1))
        if len(missing_slugs) > maximum_missing and not os.environ.get('FORCE_FETCH'):
            print(f"Refusing to overwrite: {len(missing_slugs)} previously fetched posts "
                  f"disappeared (safety limit: {maximum_missing}). Set FORCE_FETCH=1 to override.")
            sys.exit(1)
        if prev_count and len(all_posts) < prev_count:
            print(f"Warning: feed decreased from {prev_count} to {len(all_posts)} posts; "
                  "accepting the complete snapshot (likely a deletion/unpublish).")

    # Keep the full local corpus untracked and a small metadata index tracked.
    # The latter lets the deployed site show new articles even when no trade is
    # extracted from them.
    article_index = [article_metadata(post) for post in all_posts]
    atomic_write_json(POSTS_PATH, all_posts)
    atomic_write_json(ARTICLE_INDEX_PATH, article_index)

    print(f"Saved to {POSTS_PATH}")
    print(f"Saved article index to {ARTICLE_INDEX_PATH}")

    # Print summary
    for p in all_posts[:5]:
        print(f"  {(p.get('post_date') or '')[:10]} | {p.get('title', '')[:60]} | {p.get('wordcount', 0)} words")
    if len(all_posts) > 5:
        print(f"\n... and {len(all_posts)-5} more")

    # Check how many have full content
    with_content = sum(1 for p in all_posts if len(p['body_text']) > 500)
    print(f"Posts with substantial content (>500 chars): {with_content}/{len(all_posts)}")


if __name__ == '__main__':
    main()
