#!/usr/bin/env python3
"""Verify that one GitHub Actions run is an authoritative rollback source."""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple


AUTHORIZED_EVENTS = frozenset({'push', 'workflow_dispatch'})
EXPECTED_STATUS = 'completed'
EXPECTED_CONCLUSION = 'success'
EXPECTED_BRANCH = 'main'
DEPLOY_JOB_NAME = 'Deploy production'
DEPLOY_STEP_NAME = 'Deploy GitHub Pages artifact'
SMOKE_STEP_NAME = 'Verify exact release is live'
MAX_RUN_JSON_BYTES = 1_000_000
MAX_JOBS_JSON_BYTES = 10_000_000
MAX_LATEST_JOBS = 100
MAX_IDENTIFIER = 2 ** 63 - 1
REVISION_RE = re.compile(r'^[0-9a-f]{40}$')


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


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


def _read_json_object(
        path: Path,
        label: str,
        maximum_bytes: int,
) -> Mapping[str, Any]:
    _require(
        path.is_file() and not path.is_symlink(),
        f'{label} is not a regular file',
    )
    try:
        with path.open('rb') as handle:
            raw = handle.read(maximum_bytes + 1)
    except OSError as exc:
        raise ValueError(f'could not read {label}: {exc}') from exc
    _require(
        len(raw) <= maximum_bytes,
        f'{label} exceeds {maximum_bytes} bytes',
    )
    try:
        text = raw.decode('utf-8')
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(
            f'{label} is not strict UTF-8 JSON: {exc}'
        ) from exc
    _require(isinstance(value, Mapping), f'{label} must be an object')
    return value


def _positive_identifier(value: Any, label: str) -> int:
    _require(
        type(value) is int and 0 < value <= MAX_IDENTIFIER,
        f'{label} must be a positive integer',
    )
    return value


def _exact_string(value: Any, expected: str, label: str) -> str:
    _require(
        isinstance(value, str) and value == expected,
        f'{label} does not match the expected value',
    )
    return value


def _object(value: Any, label: str) -> Mapping[str, Any]:
    _require(isinstance(value, Mapping), f'{label} must be an object')
    return value


def _array(value: Any, label: str) -> List[Any]:
    _require(isinstance(value, list), f'{label} must be an array')
    return value


def _validate_required_step(
        steps: Sequence[Any],
        required_name: str,
) -> None:
    matching = []
    seen_numbers: Set[int] = set()
    for index, raw_step in enumerate(steps):
        step = _object(raw_step, f'deploy step {index}')
        number = _positive_identifier(
            step.get('number'),
            f'deploy step {index} number',
        )
        _require(
            number not in seen_numbers,
            'deploy job contains duplicate step numbers',
        )
        seen_numbers.add(number)
        name = step.get('name')
        _require(
            isinstance(name, str) and bool(name),
            f'deploy step {index} name must be a non-empty string',
        )
        if name == required_name:
            matching.append(step)
    _require(
        len(matching) == 1,
        f'deploy job must contain exactly one {required_name!r} step',
    )
    step = matching[0]
    _exact_string(
        step.get('status'),
        EXPECTED_STATUS,
        f'{required_name} step status',
    )
    _exact_string(
        step.get('conclusion'),
        EXPECTED_CONCLUSION,
        f'{required_name} step conclusion',
    )


def verify_workflow_run(
        run: Mapping[str, Any],
        jobs_document: Mapping[str, Any],
        *,
        expected_workflow_id: int,
        expected_events: Sequence[str],
        expected_head_sha: str,
) -> Mapping[str, Any]:
    """Return the proven deploy job or fail closed on any authority drift."""
    expected_workflow_id = _positive_identifier(
        expected_workflow_id,
        'expected workflow ID',
    )
    _require(
        REVISION_RE.fullmatch(expected_head_sha) is not None,
        'expected head SHA must be an exact lowercase commit SHA',
    )
    event_list = list(expected_events)
    _require(bool(event_list), 'at least one expected event is required')
    _require(
        len(event_list) == len(set(event_list))
        and all(event in AUTHORIZED_EVENTS for event in event_list),
        'expected events must be unique authorized release events',
    )

    run_id = _positive_identifier(run.get('id'), 'workflow run ID')
    run_attempt = _positive_identifier(
        run.get('run_attempt'),
        'workflow run attempt',
    )
    _require(
        _positive_identifier(run.get('workflow_id'), 'workflow ID')
        == expected_workflow_id,
        'workflow ID does not match the expected value',
    )
    event = run.get('event')
    _require(
        isinstance(event, str) and event in event_list,
        'workflow event does not match an expected release event',
    )
    _exact_string(
        run.get('status'),
        EXPECTED_STATUS,
        'workflow run status',
    )
    _exact_string(
        run.get('conclusion'),
        EXPECTED_CONCLUSION,
        'workflow run conclusion',
    )
    _exact_string(
        run.get('head_branch'),
        EXPECTED_BRANCH,
        'workflow run branch',
    )
    _exact_string(
        run.get('head_sha'),
        expected_head_sha,
        'workflow run head SHA',
    )

    total_count = jobs_document.get('total_count')
    _require(
        type(total_count) is int and 0 <= total_count <= MAX_LATEST_JOBS,
        'latest jobs total_count is invalid or requires pagination',
    )
    jobs = _array(jobs_document.get('jobs'), 'latest jobs')
    _require(
        len(jobs) == total_count,
        'latest jobs response is incomplete',
    )

    seen_job_ids: Set[int] = set()
    deploy_jobs = []
    for index, raw_job in enumerate(jobs):
        job = _object(raw_job, f'latest job {index}')
        job_id = _positive_identifier(job.get('id'), f'latest job {index} ID')
        _require(
            job_id not in seen_job_ids,
            'latest jobs response contains duplicate job IDs',
        )
        seen_job_ids.add(job_id)
        _require(
            _positive_identifier(
                job.get('run_id'),
                f'latest job {index} run ID',
            ) == run_id,
            'latest job belongs to another workflow run',
        )
        _require(
            _positive_identifier(
                job.get('run_attempt'),
                f'latest job {index} run attempt',
            ) == run_attempt,
            'latest jobs response contains a non-latest run attempt',
        )
        _exact_string(
            job.get('head_sha'),
            expected_head_sha,
            f'latest job {index} head SHA',
        )
        name = job.get('name')
        _require(
            isinstance(name, str) and bool(name),
            f'latest job {index} name must be a non-empty string',
        )
        if name == DEPLOY_JOB_NAME:
            deploy_jobs.append(job)

    _require(
        len(deploy_jobs) == 1,
        f'latest jobs must contain exactly one {DEPLOY_JOB_NAME!r} job',
    )
    deploy_job = deploy_jobs[0]
    _exact_string(
        deploy_job.get('status'),
        EXPECTED_STATUS,
        'deploy job status',
    )
    _exact_string(
        deploy_job.get('conclusion'),
        EXPECTED_CONCLUSION,
        'deploy job conclusion',
    )
    steps = _array(deploy_job.get('steps'), 'deploy job steps')
    _validate_required_step(steps, DEPLOY_STEP_NAME)
    _validate_required_step(steps, SMOKE_STEP_NAME)
    return deploy_job


def verify_files(
        run_json: Path,
        jobs_json: Path,
        *,
        expected_workflow_id: int,
        expected_events: Sequence[str],
        expected_head_sha: str,
) -> Mapping[str, Any]:
    """Strict-read downloaded API responses and verify rollback authority."""
    run = _read_json_object(
        run_json,
        'workflow run JSON',
        MAX_RUN_JSON_BYTES,
    )
    jobs = _read_json_object(
        jobs_json,
        'latest jobs JSON',
        MAX_JOBS_JSON_BYTES,
    )
    return verify_workflow_run(
        run,
        jobs,
        expected_workflow_id=expected_workflow_id,
        expected_events=expected_events,
        expected_head_sha=expected_head_sha,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            'Verify a successful GitHub Pages deployment before rollback.'
        ),
    )
    parser.add_argument('--run-json', type=Path, required=True)
    parser.add_argument('--jobs-json', type=Path, required=True)
    parser.add_argument('--expected-workflow-id', type=int, required=True)
    parser.add_argument(
        '--expected-event',
        action='append',
        choices=sorted(AUTHORIZED_EVENTS),
        required=True,
    )
    parser.add_argument('--expected-head-sha', required=True)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        deploy_job = verify_files(
            args.run_json,
            args.jobs_json,
            expected_workflow_id=args.expected_workflow_id,
            expected_events=args.expected_event,
            expected_head_sha=args.expected_head_sha,
        )
    except ValueError as exc:
        print(f'WORKFLOW RUN VERIFICATION FAILED: {exc}', file=sys.stderr)
        return 1
    print(
        'Workflow run metadata passed: '
        f'deploy job {deploy_job["id"]} completed exact live verification.'
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
