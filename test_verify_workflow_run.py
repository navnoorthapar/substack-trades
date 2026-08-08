"""Regression tests for rollback-source GitHub Actions metadata."""

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import verify_workflow_run


REVISION = 'a' * 40
WORKFLOW_ID = 123_456
RUN_ID = 789_012
RUN_ATTEMPT = 2


def run_fixture():
    return {
        'id': RUN_ID,
        'workflow_id': WORKFLOW_ID,
        'event': 'push',
        'status': 'completed',
        'conclusion': 'success',
        'head_branch': 'main',
        'head_sha': REVISION,
        'run_attempt': RUN_ATTEMPT,
    }


def deploy_job_fixture():
    return {
        'id': 900_001,
        'run_id': RUN_ID,
        'run_attempt': RUN_ATTEMPT,
        'head_sha': REVISION,
        'name': 'Deploy production',
        'status': 'completed',
        'conclusion': 'success',
        'steps': [
            {
                'number': 1,
                'name': 'Set up job',
                'status': 'completed',
                'conclusion': 'success',
            },
            {
                'number': 2,
                'name': 'Check out smoke test and expected snapshot',
                'status': 'completed',
                'conclusion': 'success',
            },
            {
                'number': 3,
                'name': 'Reconfirm release still owns main',
                'status': 'completed',
                'conclusion': 'success',
            },
            {
                'number': 4,
                'name': 'Deploy GitHub Pages artifact',
                'status': 'completed',
                'conclusion': 'success',
            },
            {
                'number': 5,
                'name': 'Verify exact release is live',
                'status': 'completed',
                'conclusion': 'success',
            },
            {
                'number': 6,
                'name': 'Prove post-deploy authority',
                'status': 'completed',
                'conclusion': 'success',
            },
            {
                'number': 7,
                'name': 'Reconcile Pages deployment outcome',
                'status': 'completed',
                'conclusion': 'success',
            },
        ],
    }


def jobs_fixture():
    return {
        'total_count': 1,
        'jobs': [deploy_job_fixture()],
    }


class VerifyWorkflowRunTests(unittest.TestCase):
    def verify(self, run=None, jobs=None, **overrides):
        arguments = {
            'expected_workflow_id': WORKFLOW_ID,
            'expected_events': ('push', 'workflow_dispatch'),
            'expected_head_sha': REVISION,
        }
        arguments.update(overrides)
        return verify_workflow_run.verify_workflow_run(
            run if run is not None else run_fixture(),
            jobs if jobs is not None else jobs_fixture(),
            **arguments,
        )

    def test_accepts_one_complete_latest_successful_deploy(self):
        deploy_job = self.verify()
        self.assertEqual(deploy_job['id'], 900_001)

        alternate = run_fixture()
        alternate['event'] = 'workflow_dispatch'
        self.assertEqual(self.verify(run=alternate)['id'], 900_001)

    def test_accepts_the_hosted_phase_one_jobs_api_certificate(self):
        # GitHub's Jobs API reports continued step conclusions as success. The
        # ordered smoke, authority, and reconciliation steps are the separate
        # evidence that exact live bytes still belonged to this release.
        required = [
            step for step in jobs_fixture()['jobs'][0]['steps']
            if step['name'] in {
                'Deploy GitHub Pages artifact',
                'Verify exact release is live',
                'Prove post-deploy authority',
                'Reconcile Pages deployment outcome',
            }
        ]
        self.assertEqual([step['number'] for step in required], [4, 5, 6, 7])
        self.assertEqual(
            [step['conclusion'] for step in required],
            ['success', 'success', 'success', 'success'],
        )
        self.assertEqual(self.verify()['id'], 900_001)

    def test_rejects_every_run_authority_mismatch(self):
        cases = (
            ('workflow_id', WORKFLOW_ID + 1, 'workflow ID'),
            ('event', 'schedule', 'workflow event'),
            ('status', 'in_progress', 'run status'),
            ('conclusion', 'failure', 'run conclusion'),
            ('head_branch', 'release', 'run branch'),
            ('head_sha', 'b' * 40, 'run head SHA'),
        )
        for field, value, message in cases:
            with self.subTest(field=field):
                run = run_fixture()
                run[field] = value
                with self.assertRaisesRegex(ValueError, message):
                    self.verify(run=run)

        with self.assertRaisesRegex(ValueError, 'workflow event'):
            self.verify(expected_events=('workflow_dispatch',))

    def test_rejects_unauthorized_deploy_smoke_or_reconciliation(self):
        cases = (
            ('Deploy GitHub Pages artifact', 'completed', 'failure', 'conclusion'),
            ('Deploy GitHub Pages artifact', 'completed', 'skipped', 'conclusion'),
            ('Deploy GitHub Pages artifact', 'completed', 'cancelled', 'conclusion'),
            ('Verify exact release is live', 'completed', 'failure', 'conclusion'),
            ('Verify exact release is live', 'in_progress', None, 'status'),
            ('Prove post-deploy authority', 'completed', 'failure', 'conclusion'),
            ('Prove post-deploy authority', 'completed', 'skipped', 'conclusion'),
            ('Prove post-deploy authority', 'in_progress', None, 'status'),
            ('Reconcile Pages deployment outcome', 'completed', 'failure', 'conclusion'),
            ('Reconcile Pages deployment outcome', 'completed', 'skipped', 'conclusion'),
            ('Reconcile Pages deployment outcome', 'in_progress', None, 'status'),
        )
        for name, status, conclusion, message in cases:
            with self.subTest(name=name, conclusion=conclusion):
                jobs = jobs_fixture()
                step = next(
                    item for item in jobs['jobs'][0]['steps']
                    if item['name'] == name
                )
                step['status'] = status
                step['conclusion'] = conclusion
                with self.assertRaisesRegex(ValueError, message):
                    self.verify(jobs=jobs)

    def test_rejects_missing_or_duplicate_required_steps_and_jobs(self):
        for required_name in (
            'Deploy GitHub Pages artifact',
            'Verify exact release is live',
            'Prove post-deploy authority',
            'Reconcile Pages deployment outcome',
        ):
            with self.subTest(required_name=required_name, case='missing'):
                jobs = jobs_fixture()
                jobs['jobs'][0]['steps'] = [
                    step for step in jobs['jobs'][0]['steps']
                    if step['name'] != required_name
                ]
                with self.assertRaisesRegex(ValueError, 'exactly one'):
                    self.verify(jobs=jobs)

            with self.subTest(required_name=required_name, case='duplicate'):
                jobs = jobs_fixture()
                duplicate_step = copy.deepcopy(next(
                    step for step in jobs['jobs'][0]['steps']
                    if step['name'] == required_name
                ))
                duplicate_step['number'] = 10
                jobs['jobs'][0]['steps'].append(duplicate_step)
                with self.assertRaisesRegex(ValueError, 'exactly one'):
                    self.verify(jobs=jobs)

        jobs = jobs_fixture()
        duplicate_job = copy.deepcopy(jobs['jobs'][0])
        duplicate_job['id'] = 900_002
        jobs['jobs'].append(duplicate_job)
        jobs['total_count'] = 2
        with self.assertRaisesRegex(ValueError, 'exactly one'):
            self.verify(jobs=jobs)

    def test_rejects_required_verification_steps_out_of_order(self):
        jobs = jobs_fixture()
        smoke = next(
            step for step in jobs['jobs'][0]['steps']
            if step['name'] == 'Verify exact release is live'
        )
        authority = next(
            step for step in jobs['jobs'][0]['steps']
            if step['name'] == 'Prove post-deploy authority'
        )
        smoke['number'], authority['number'] = (
            authority['number'], smoke['number']
        )
        with self.assertRaisesRegex(ValueError, 'out of order'):
            self.verify(jobs=jobs)

    def test_rejects_incomplete_or_paginated_latest_jobs(self):
        jobs = jobs_fixture()
        jobs['total_count'] = 2
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            self.verify(jobs=jobs)

        jobs = jobs_fixture()
        jobs['total_count'] = 101
        with self.assertRaisesRegex(ValueError, 'requires pagination'):
            self.verify(jobs=jobs)

    def test_rejects_duplicate_job_ids_and_nonlatest_or_foreign_jobs(self):
        extra_job = {
            'id': 900_002,
            'run_id': RUN_ID,
            'run_attempt': RUN_ATTEMPT,
            'head_sha': REVISION,
            'name': 'Quality gate',
        }
        cases = (
            ('id', 900_001, 'duplicate job IDs'),
            ('run_id', RUN_ID + 1, 'another workflow run'),
            ('run_attempt', RUN_ATTEMPT - 1, 'non-latest run attempt'),
            ('head_sha', 'b' * 40, 'job 1 head SHA'),
        )
        for field, value, message in cases:
            with self.subTest(field=field):
                jobs = jobs_fixture()
                changed = copy.deepcopy(extra_job)
                changed[field] = value
                jobs['jobs'].append(changed)
                jobs['total_count'] = 2
                with self.assertRaisesRegex(ValueError, message):
                    self.verify(jobs=jobs)

    def test_strict_json_rejects_duplicates_constants_and_bad_utf8(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            run_path = directory / 'run.json'
            jobs_path = directory / 'jobs.json'
            jobs_path.write_text(json.dumps(jobs_fixture()), encoding='utf-8')

            for payload, message in (
                (b'{"id": 1, "id": 2}', 'duplicate JSON object key'),
                (b'{"id": NaN}', 'non-standard JSON constant'),
                (b'\xff', 'UTF-8'),
            ):
                with self.subTest(message=message):
                    run_path.write_bytes(payload)
                    with self.assertRaisesRegex(ValueError, message):
                        verify_workflow_run.verify_files(
                            run_path,
                            jobs_path,
                            expected_workflow_id=WORKFLOW_ID,
                            expected_events=('push',),
                            expected_head_sha=REVISION,
                        )

    def test_cli_reports_success_and_failure_without_tracebacks(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            run_path = directory / 'run.json'
            jobs_path = directory / 'jobs.json'
            run_path.write_text(json.dumps(run_fixture()), encoding='utf-8')
            jobs_path.write_text(json.dumps(jobs_fixture()), encoding='utf-8')
            arguments = [
                '--run-json', str(run_path),
                '--jobs-json', str(jobs_path),
                '--expected-workflow-id', str(WORKFLOW_ID),
                '--expected-event', 'push',
                '--expected-head-sha', REVISION,
            ]

            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(verify_workflow_run.main(arguments), 0)
            self.assertIn('metadata passed', output.getvalue())

            failed = run_fixture()
            failed['conclusion'] = 'failure'
            run_path.write_text(json.dumps(failed), encoding='utf-8')
            with redirect_stderr(io.StringIO()) as error:
                self.assertEqual(verify_workflow_run.main(arguments), 1)
            self.assertIn('VERIFICATION FAILED', error.getvalue())


if __name__ == '__main__':
    unittest.main()
