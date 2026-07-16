#!/usr/bin/env python3
"""Fail closed unless every generated inline script parses as JavaScript."""

import argparse
import subprocess
import tempfile
from html.parser import HTMLParser
from pathlib import Path


class InlineScriptParser(HTMLParser):
    """Collect executable inline scripts without interpreting their contents."""

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.scripts = []
        self.external_sources = []
        self._parts = None
        self._script_type = ''

    def handle_starttag(self, tag, attrs):
        if tag.casefold() != 'script':
            return
        attributes = {name.casefold(): value for name, value in attrs}
        source = attributes.get('src')
        if source:
            self.external_sources.append(source)
            self._parts = None
            return
        self._script_type = str(attributes.get('type') or '').strip().casefold()
        self._parts = []

    def handle_data(self, data):
        if self._parts is not None:
            self._parts.append(data)

    def handle_endtag(self, tag):
        if tag.casefold() != 'script' or self._parts is None:
            return
        self.scripts.append((self._script_type, ''.join(self._parts)))
        self._parts = None
        self._script_type = ''


def validate_inline_scripts(html_path, node='node'):
    """Parse every executable inline script with Node's syntax checker."""
    path = Path(html_path)
    parser = InlineScriptParser()
    parser.feed(path.read_text(encoding='utf-8'))
    parser.close()
    if parser._parts is not None:
        raise ValueError('generated site contains an unterminated inline script')
    if parser.external_sources:
        raise ValueError('generated site must not depend on external scripts')
    executable_types = {'', 'text/javascript', 'application/javascript', 'module'}
    scripts = [
        (script_type, source)
        for script_type, source in parser.scripts
        if script_type in executable_types
    ]
    if not scripts:
        raise ValueError('generated site contains no executable inline scripts')

    with tempfile.TemporaryDirectory(prefix='nrt-inline-js-') as directory:
        for index, (script_type, source) in enumerate(scripts, start=1):
            suffix = '.mjs' if script_type == 'module' else '.js'
            script_path = Path(directory) / f'inline-{index}{suffix}'
            script_path.write_text(source, encoding='utf-8')
            try:
                result = subprocess.run(
                    [node, '--check', str(script_path)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
            except FileNotFoundError as exc:
                raise ValueError('Node.js is required for JavaScript syntax validation') from exc
            if result.returncode:
                detail = (result.stderr or result.stdout).strip()
                raise ValueError(
                    f'inline script {index} failed JavaScript syntax validation: {detail}'
                )
    return len(scripts)


def main():
    parser = argparse.ArgumentParser(
        description='Validate every executable inline script in generated HTML.',
    )
    parser.add_argument('html', type=Path)
    args = parser.parse_args()
    try:
        count = validate_inline_scripts(args.html)
    except (OSError, UnicodeError, ValueError) as exc:
        parser.exit(1, f'INLINE SCRIPT VALIDATION FAILED: {exc}\n')
    print(f'Inline JavaScript syntax passed: {count} script(s).')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
