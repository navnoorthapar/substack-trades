#!/usr/bin/env python3
"""
Fetch all posts from navnoorbawa.substack.com API and save content locally.
"""
import urllib.request
import json
import time
import html
import re
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).parent

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/html, */*',
}

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


def fetch_posts(limit=50, offset=0):
    url = f'https://navnoorbawa.substack.com/api/v1/posts?limit={limit}&offset={offset}'
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def main():
    all_posts = []
    offset = 0
    limit = 50

    print("Fetching posts from Substack API...")

    while True:
        print(f"  Fetching offset={offset}...", end=' ', flush=True)
        try:
            posts = fetch_posts(limit=limit, offset=offset)
        except Exception as e:
            print(f"Error: {e}")
            break

        if not posts:
            print("No more posts.")
            break

        print(f"Got {len(posts)} posts")

        for post in posts:
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

    # Save to file
    output_path = ROOT / 'all_posts.json'
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(all_posts, f, ensure_ascii=False, indent=2)

    print(f"Saved to {output_path}")

    # Print summary
    for p in all_posts[:5]:
        print(f"  {p['post_date'][:10]} | {p['title'][:60]} | {p['wordcount']} words")

    print(f"\n... and {len(all_posts)-5} more")

    # Check how many have full content
    with_content = sum(1 for p in all_posts if len(p['body_text']) > 500)
    print(f"Posts with substantial content (>500 chars): {with_content}/{len(all_posts)}")


if __name__ == '__main__':
    main()
