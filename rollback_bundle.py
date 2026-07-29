#!/usr/bin/env python3
"""Create and verify an immutable, previously validated Pages rollback bundle."""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import http.client
import json
import os
import re
import shutil
import tarfile
import time
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple


# This is the durable rollback-envelope version, not the evolving release/data
# schema. Keep version 2 verification compatible for at least the full artifact
# retention window; release fingerprint names are intentionally generic below.
BUNDLE_SCHEMA_VERSION = 2
ATTESTATION_NAME = 'release-attestation.json'
SOURCE_NAMES = (
    'articles_index.json',
    'trades_extracted.json',
    'snapshot_manifest.json',
)
SOCIAL_IMAGE_RELATIVE = Path('assets') / 'og.jpg'
REVISION_RE = re.compile(r'^[0-9a-f]{40}$')
SHA256_RE = re.compile(r'^[0-9a-f]{64}$')
MAX_ARCHIVE_FILES = 10_000
MAX_ARCHIVE_BYTES = 100_000_000
MAX_SITE_PATH_LENGTH = 1_024
MAX_SMOKE_RETRIES = 12
MAX_SMOKE_DELAY_SECONDS = 60.0
MAX_SMOKE_TIMEOUT_SECONDS = 120.0
MAX_SMOKE_CONCURRENCY = 32

HttpsOrigin = Tuple[str, int]


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _validate_current_release(
        site: Path,
        articles: Path,
        observations: Path,
        manifest: Path,
        revision: str,
) -> Mapping[str, str]:
    """Load the evolving release contract only for current-release checks."""
    from validate_release import validate_release

    return validate_release(
        site,
        articles,
        observations,
        manifest,
        revision,
    )


def _reject_json_constant(value: str) -> None:
    raise ValueError(f'non-standard JSON constant {value!r}')


def _unique_object(
        pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    value: Dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f'duplicate JSON object key {key!r}')
        value[key] = item
    return value


def _read_attestation(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding='utf-8'),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(
            f'rollback attestation is not strict UTF-8 JSON: {exc}'
        ) from exc
    _require(isinstance(value, Mapping),
             'rollback attestation must be an object')
    return value


def _regular_files(root: Path) -> List[Path]:
    _require(root.is_dir() and not root.is_symlink(),
             f'rollback payload directory is invalid: {root.name}')
    files = []
    for path in root.rglob('*'):
        _require(not path.is_symlink(),
                 f'rollback payload contains a symlink: {path}')
        _require(path.is_dir() or path.is_file(),
                 f'rollback payload contains a special file: {path}')
        if path.is_file():
            files.append(path)
    return sorted(files, key=lambda path: path.relative_to(root).as_posix())


def payload_checksum(bundle: Path) -> str:
    """Hash exact relative paths, lengths, and bytes under site/ and source/."""
    digest = hashlib.sha256()
    for directory_name in ('site', 'source'):
        directory = bundle / directory_name
        for path in _regular_files(directory):
            relative = path.relative_to(bundle).as_posix()
            data = path.read_bytes()
            digest.update(relative.encode('utf-8'))
            digest.update(b'\0')
            digest.update(str(len(data)).encode('ascii'))
            digest.update(b'\0')
            digest.update(data)
            digest.update(b'\0')
    return digest.hexdigest()


def site_file_checksums(site: Path) -> Dict[str, str]:
    """Return the exact path-to-hash manifest used for schema-neutral smoke."""
    files = _regular_files(site)
    _require(bool(files), 'rollback site payload is empty')
    _require(len(files) <= MAX_ARCHIVE_FILES,
             'rollback site payload contains too many files')
    return {
        path.relative_to(site).as_posix():
            hashlib.sha256(path.read_bytes()).hexdigest()
        for path in files
    }


def _copy_regular_tree(source: Path, destination: Path) -> None:
    _regular_files(source)
    shutil.copytree(source, destination, symlinks=False)


def _write_attestation(
        path: Path,
        revision: str,
        fingerprints: Mapping[str, str],
        payload_sha256: str,
        site_files: Mapping[str, str],
) -> None:
    value = {
        'schema_version': BUNDLE_SCHEMA_VERSION,
        'revision': revision,
        'fingerprints': dict(fingerprints),
        'payload_sha256': payload_sha256,
        'site_files': dict(site_files),
    }
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8',
    )


def create_bundle(
        site: Path,
        articles: Path,
        observations: Path,
        manifest: Path,
        revision: str,
        output: Path,
) -> Mapping[str, str]:
    """Validate current inputs, then archive their exact artifact and sources."""
    _require(REVISION_RE.fullmatch(revision) is not None,
             'rollback revision must be an exact lowercase commit SHA')
    _require(not output.exists(),
             'rollback bundle output already exists')
    fingerprints = _validate_current_release(
        site,
        articles,
        observations,
        manifest,
        revision,
    )
    temporary = output.with_name(
        f'.{output.name}.tmp-{os.getpid()}'
    )
    _require(not temporary.exists(),
             'rollback bundle temporary output already exists')
    try:
        temporary.mkdir(parents=True)
        _copy_regular_tree(site, temporary / 'site')
        source_dir = temporary / 'source'
        source_dir.mkdir()
        for source, name in (
            (articles, SOURCE_NAMES[0]),
            (observations, SOURCE_NAMES[1]),
            (manifest, SOURCE_NAMES[2]),
        ):
            _require(source.is_file() and not source.is_symlink(),
                     f'rollback source is not a regular file: {name}')
            shutil.copyfile(source, source_dir / name)
        social_image = articles.parent / SOCIAL_IMAGE_RELATIVE
        _require(
            social_image.is_file() and not social_image.is_symlink(),
            'tracked rollback social image is not a regular file',
        )
        archived_social = source_dir / SOCIAL_IMAGE_RELATIVE
        archived_social.parent.mkdir()
        shutil.copyfile(social_image, archived_social)
        digest = payload_checksum(temporary)
        site_files = site_file_checksums(temporary / 'site')
        _write_attestation(
            temporary / ATTESTATION_NAME,
            revision,
            fingerprints,
            digest,
            site_files,
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        os.replace(temporary, output)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return fingerprints


def _write_github_output(
        path: Path,
        fingerprints: Mapping[str, str],
) -> None:
    with path.open('a', encoding='utf-8') as handle:
        for key, value in fingerprints.items():
            _require(
                re.fullmatch(r'[a-z][a-z0-9_]{0,127}', key) is not None
                and SHA256_RE.fullmatch(value) is not None,
                'release fingerprint cannot be written to GitHub output',
            )
            handle.write(f'{key}={value}\n')


def verify_attestation(
        bundle: Path,
        expected_revision: str,
) -> Tuple[Mapping[str, str], Mapping[str, str]]:
    """Verify exact archived bytes without interpreting their release schema."""
    _require(REVISION_RE.fullmatch(expected_revision) is not None,
             'rollback revision must be an exact lowercase commit SHA')
    _require(bundle.is_dir() and not bundle.is_symlink(),
             'rollback bundle is not a regular directory')
    attestation_path = bundle / ATTESTATION_NAME
    _require(
        {path.name for path in bundle.iterdir()}
        == {'site', 'source', ATTESTATION_NAME},
        'rollback bundle has the wrong root entry set',
    )
    _require(
        attestation_path.is_file() and not attestation_path.is_symlink(),
        'rollback attestation is not a regular file',
    )
    source = bundle / 'source'
    _require(
        source.is_dir()
        and not source.is_symlink()
        and {path.name for path in source.iterdir()}
        == set(SOURCE_NAMES) | {'assets'},
        'rollback bundle has the wrong source entry set',
    )
    _require(
        all(
            (source / name).is_file()
            and not (source / name).is_symlink()
            for name in SOURCE_NAMES
        ),
        'rollback bundle source snapshots are not regular files',
    )
    assets = source / 'assets'
    _require(
        assets.is_dir()
        and not assets.is_symlink()
        and {path.name for path in assets.iterdir()} == {'og.jpg'}
        and (assets / 'og.jpg').is_file()
        and not (assets / 'og.jpg').is_symlink(),
        'rollback bundle has the wrong tracked support-asset set',
    )
    _regular_files(bundle / 'site')
    _regular_files(source)

    attestation = _read_attestation(attestation_path)
    _require(
        set(attestation)
        == {
            'schema_version',
            'revision',
            'fingerprints',
            'payload_sha256',
            'site_files',
        },
        'rollback attestation has the wrong field set',
    )
    _require(
        attestation.get('schema_version') == BUNDLE_SCHEMA_VERSION,
        'rollback attestation has an unsupported schema version',
    )
    _require(attestation.get('revision') == expected_revision,
             'rollback attestation revision does not match the request')
    attested_fingerprints = attestation.get('fingerprints')
    _require(
        isinstance(attested_fingerprints, Mapping)
        and bool(attested_fingerprints)
        and all(
            isinstance(key, str)
            and bool(key)
            and len(key) <= 128
            and key == key.strip()
            and not any(
                ord(character) < 32 or ord(character) == 127
                for character in key
            )
            and isinstance(value, str)
            and SHA256_RE.fullmatch(value) is not None
            for key, value in attested_fingerprints.items()
        ),
        'rollback attestation fingerprints are invalid',
    )
    if not isinstance(attested_fingerprints, Mapping):
        raise ValueError('rollback attestation fingerprints are invalid')
    normalized_attested = {
        str(key): str(value)
        for key, value in attested_fingerprints.items()
    }
    calculated_payload = payload_checksum(bundle)
    _require(
        attestation.get('payload_sha256') == calculated_payload,
        'rollback payload checksum does not match its attestation',
    )
    attested_site_files = attestation.get('site_files')
    _require(
        isinstance(attested_site_files, Mapping)
        and bool(attested_site_files)
        and len(attested_site_files) <= MAX_ARCHIVE_FILES
        and all(
            isinstance(path, str)
            and _canonical_site_path(path) is not None
            and isinstance(digest, str)
            and SHA256_RE.fullmatch(digest) is not None
            for path, digest in attested_site_files.items()
        ),
        'rollback attestation site-file manifest is invalid',
    )
    if not isinstance(attested_site_files, Mapping):
        raise ValueError(
            'rollback attestation site-file manifest is invalid'
        )
    normalized_site_files = {
        str(path): str(digest)
        for path, digest in attested_site_files.items()
    }
    _require(
        normalized_site_files == site_file_checksums(bundle / 'site'),
        'rollback site files do not match their attestation',
    )
    return normalized_attested, normalized_site_files


def validate_bundle(
        bundle: Path,
        expected_revision: str,
        *,
        github_output: Optional[Path] = None,
) -> Mapping[str, str]:
    """Re-run the current full release contract on a freshly copied bundle."""
    normalized_attested, _ = verify_attestation(
        bundle,
        expected_revision,
    )
    source = bundle / 'source'

    calculated_fingerprints = _validate_current_release(
        bundle / 'site',
        source / SOURCE_NAMES[0],
        source / SOURCE_NAMES[1],
        source / SOURCE_NAMES[2],
        expected_revision,
    )
    _require(
        normalized_attested == dict(calculated_fingerprints),
        'rollback release fingerprints do not match their attestation',
    )
    if github_output is not None:
        _write_github_output(github_output, calculated_fingerprints)
    return calculated_fingerprints


def _canonical_site_path(name: str) -> Optional[Path]:
    """Return a strict canonical relative path used by the URL verifier."""
    if (
        not name
        or len(name) > MAX_SITE_PATH_LENGTH
        or '\\' in name
        or any(
            ord(character) < 32 or ord(character) == 127
            for character in name
        )
    ):
        return None
    pure = PurePosixPath(name)
    if (
        pure.is_absolute()
        or pure.as_posix() != name
        or any(part in ('', '.', '..') for part in pure.parts)
    ):
        return None
    return Path(*pure.parts)


def _https_origin(url: str, label: str) -> HttpsOrigin:
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f'{label} is not a valid HTTPS URL: {exc}') from exc
    hostname = parsed.hostname
    _require(
        parsed.scheme.casefold() == 'https'
        and hostname is not None
        and parsed.username is None
        and parsed.password is None,
        f'{label} must be an authenticated-free HTTPS URL',
    )
    if hostname is None:
        raise ValueError(f'{label} has no hostname')
    return hostname.casefold(), port or 443


class _SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject a redirect before urllib can make an off-origin request."""

    def __init__(self, origin: HttpsOrigin):
        super().__init__()
        self.origin = origin

    def redirect_request(
            self,
            req: urllib.request.Request,
            fp: Any,
            code: int,
            msg: str,
            headers: Any,
            newurl: str) -> Optional[urllib.request.Request]:
        absolute_url = urllib.parse.urljoin(req.full_url, newurl)
        _require(
            _https_origin(absolute_url, 'rollback redirect target')
            == self.origin,
            'rollback smoke refused an off-origin redirect',
        )
        return super().redirect_request(
            req, fp, code, msg, headers, absolute_url,
        )


def _open_same_origin(
        request: urllib.request.Request,
        origin: HttpsOrigin,
        timeout: float) -> Any:
    opener = urllib.request.build_opener(
        _SameOriginRedirectHandler(origin),
    )
    return opener.open(request, timeout=timeout)


def _site_url(
        base_url: str,
        relative: str,
        expected_revision: str,
) -> str:
    parsed = urllib.parse.urlsplit(base_url)
    base_path = parsed.path.rstrip('/') + '/'
    return urllib.parse.urlunsplit((
        parsed.scheme,
        parsed.netloc,
        base_path + urllib.parse.quote(relative, safe='/'),
        urllib.parse.urlencode({'rollback-revision': expected_revision}),
        '',
    ))


def _fetch_site_file(
        base_url: str,
        origin: HttpsOrigin,
        relative: str,
        expected_bytes: bytes,
        expected_sha256: str,
        expected_revision: str,
        *,
        retries: int,
        retry_delay: float,
        timeout: float,
) -> None:
    expected_length = len(expected_bytes)
    _require(
        expected_length <= MAX_ARCHIVE_BYTES,
        f'rollback site file exceeds the smoke size ceiling: {relative}',
    )
    url = _site_url(base_url, relative, expected_revision)
    requested_url = urllib.parse.urlsplit(url)
    last_error = 'unknown error'
    for attempt in range(1, retries + 1):
        try:
            request = urllib.request.Request(
                url,
                headers={
                    'Accept': '*/*',
                    'Accept-Encoding': 'identity',
                    'Cache-Control': 'no-cache',
                    'Pragma': 'no-cache',
                    'User-Agent': 'NavnoorRollbackVerifier/2',
                },
            )
            with _open_same_origin(request, origin, timeout) as response:
                status = getattr(response, 'status', None)
                if status is None:
                    status = response.getcode()
                _require(
                    status == 200,
                    f'HTTP status was {status}, expected 200',
                )
                final_url = response.geturl()
                final_parts = urllib.parse.urlsplit(final_url)
                _require(
                    _https_origin(
                        final_url,
                        'rollback smoke final response',
                    ) == origin
                    and final_parts.path == requested_url.path
                    and final_parts.query == requested_url.query
                    and not final_parts.fragment,
                    'rollback smoke final response changed origin, path, '
                    'or cache-busting revision',
                )
                content_encoding = response.headers.get('Content-Encoding')
                _require(
                    content_encoding is None
                    or content_encoding.casefold() == 'identity',
                    'rollback smoke received transformed response bytes',
                )
                content_length = response.headers.get('Content-Length')
                if content_length is not None:
                    _require(
                        content_length.isdigit()
                        and int(content_length) == expected_length,
                        'rollback smoke response length header did not match',
                    )
                actual = response.read(expected_length + 1)
            _require(
                len(actual) == expected_length,
                'rollback smoke response length did not match',
            )
            _require(
                actual == expected_bytes
                and hashlib.sha256(actual).hexdigest() == expected_sha256,
                'rollback smoke response bytes did not match',
            )
            return
        except (OSError, ValueError, http.client.HTTPException) as exc:
            last_error = str(exc)
            if attempt < retries and retry_delay:
                time.sleep(retry_delay)
    raise ValueError(
        f'rollback live file {relative!r} failed after '
        f'{retries} attempts: {last_error}'
    )


def smoke_bundle(
        bundle: Path,
        expected_revision: str,
        base_url: str,
        *,
        retries: int = 8,
        retry_delay: float = 10.0,
        timeout: float = 20.0,
        concurrency: int = 12,
) -> None:
    """Fetch every archived site file and compare its exact deployed bytes."""
    _require(1 <= retries <= MAX_SMOKE_RETRIES,
             'rollback smoke retries are outside the safe range')
    _require(0 <= retry_delay <= MAX_SMOKE_DELAY_SECONDS,
             'rollback smoke retry delay is outside the safe range')
    _require(0 < timeout <= MAX_SMOKE_TIMEOUT_SECONDS,
             'rollback smoke timeout is outside the safe range')
    _require(1 <= concurrency <= MAX_SMOKE_CONCURRENCY,
             'rollback smoke concurrency is outside the safe range')
    parsed = urllib.parse.urlsplit(base_url)
    origin = _https_origin(base_url, 'rollback smoke base URL')
    _require(
        not parsed.query and not parsed.fragment,
        'rollback smoke base URL cannot contain a query or fragment',
    )
    _, site_files = verify_attestation(bundle, expected_revision)
    site = bundle / 'site'
    failures = []
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(concurrency, len(site_files))) as executor:
        futures = {
            executor.submit(
                _fetch_site_file,
                base_url,
                origin,
                relative,
                (site / Path(*PurePosixPath(relative).parts)).read_bytes(),
                digest,
                expected_revision,
                retries=retries,
                retry_delay=retry_delay,
                timeout=timeout,
            ): relative
            for relative, digest in site_files.items()
        }
        for future in concurrent.futures.as_completed(futures):
            relative = futures[future]
            try:
                future.result()
            except (OSError, ValueError, http.client.HTTPException) as exc:
                failures.append(f'{relative}: {exc}')
    _require(
        not failures,
        'rollback exact-byte smoke failed: '
        + '; '.join(sorted(failures)[:10]),
    )
    print(
        f'Rollback exact-byte smoke passed for {len(site_files)} site files.'
    )


def _safe_member_path(name: str) -> Optional[Path]:
    pure = PurePosixPath(name)
    _require(not pure.is_absolute() and '\\' not in name,
             'rollback archive contains an unsafe path')
    parts = tuple(part for part in pure.parts if part not in ('', '.'))
    _require('..' not in parts,
             'rollback archive contains an unsafe path')
    if not parts:
        return None
    return Path(*parts)


def extract_pages_archive(archive_path: Path, output: Path) -> None:
    """Extract upload-pages-artifact's inner tar with traversal protection."""
    _require(archive_path.is_file() and not archive_path.is_symlink(),
             'rollback Pages archive is not a regular file')
    _require(not output.exists(),
             'rollback extraction output already exists')
    temporary = output.with_name(
        f'.{output.name}.tmp-{os.getpid()}'
    )
    _require(not temporary.exists(),
             'rollback extraction temporary output already exists')
    total_bytes = 0
    normalized_names = set()
    try:
        temporary.mkdir(parents=True)
        with tarfile.open(archive_path, mode='r:*') as archive:
            members = archive.getmembers()
            _require(len(members) <= MAX_ARCHIVE_FILES,
                     'rollback archive contains too many entries')
            for member in members:
                relative = _safe_member_path(member.name)
                if relative is None:
                    _require(member.isdir(),
                             'rollback archive root entry is not a directory')
                    continue
                normalized = relative.as_posix()
                _require(normalized not in normalized_names,
                         'rollback archive contains a duplicate path')
                normalized_names.add(normalized)
                _require(member.isdir() or member.isfile(),
                         'rollback archive contains a link or special file')
                total_bytes += member.size
                _require(total_bytes <= MAX_ARCHIVE_BYTES,
                         'rollback archive exceeds the size ceiling')
                destination = temporary / relative
                if member.isdir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise ValueError(
                        'rollback archive file could not be read'
                    )
                with source, destination.open('xb') as handle:
                    shutil.copyfileobj(source, handle)
        output.parent.mkdir(parents=True, exist_ok=True)
        os.replace(temporary, output)
    except (OSError, tarfile.TarError) as exc:
        raise ValueError(f'rollback archive extraction failed: {exc}') from exc
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            'Create, validate, attest, or exact-smoke a Pages rollback bundle.'
        ),
    )
    subparsers = parser.add_subparsers(dest='command', required=True)

    create = subparsers.add_parser('create')
    create.add_argument('--site', required=True, type=Path)
    create.add_argument('--articles', required=True, type=Path)
    create.add_argument('--trades', required=True, type=Path)
    create.add_argument('--manifest', required=True, type=Path)
    create.add_argument('--revision', required=True)
    create.add_argument('--output', required=True, type=Path)

    extract = subparsers.add_parser('extract')
    extract.add_argument('--archive', required=True, type=Path)
    extract.add_argument('--output', required=True, type=Path)

    validate = subparsers.add_parser('validate')
    validate.add_argument('--bundle', required=True, type=Path)
    validate.add_argument('--expected-revision', required=True)
    validate.add_argument('--github-output', type=Path)

    verify = subparsers.add_parser('verify-attestation')
    verify.add_argument('--bundle', required=True, type=Path)
    verify.add_argument('--expected-revision', required=True)

    smoke = subparsers.add_parser('smoke')
    smoke.add_argument('--bundle', required=True, type=Path)
    smoke.add_argument('--expected-revision', required=True)
    smoke.add_argument('--base-url', required=True)
    smoke.add_argument('--retries', type=int, default=8)
    smoke.add_argument('--retry-delay', type=float, default=10.0)
    smoke.add_argument('--timeout', type=float, default=20.0)
    smoke.add_argument('--concurrency', type=int, default=12)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == 'create':
            create_bundle(
                args.site,
                args.articles,
                args.trades,
                args.manifest,
                args.revision,
                args.output,
            )
        elif args.command == 'extract':
            extract_pages_archive(args.archive, args.output)
        elif args.command == 'validate':
            validate_bundle(
                args.bundle,
                args.expected_revision,
                github_output=args.github_output,
            )
        elif args.command == 'verify-attestation':
            verify_attestation(
                args.bundle,
                args.expected_revision,
            )
        elif args.command == 'smoke':
            smoke_bundle(
                args.bundle,
                args.expected_revision,
                args.base_url,
                retries=args.retries,
                retry_delay=args.retry_delay,
                timeout=args.timeout,
                concurrency=args.concurrency,
            )
        else:
            raise ValueError('unsupported rollback command')
    except (OSError, UnicodeError, ValueError) as exc:
        parser.exit(1, f'ROLLBACK BUNDLE FAILED: {exc}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
