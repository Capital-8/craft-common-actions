"""Exercise the reusable SonarCloud workflow's project-key trust boundary."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]


class SonarCloudTests(unittest.TestCase):
    def setUp(self):
        workflow = yaml.load(
            (ROOT / '.github/workflows/sonarcloud.yml').read_text(),
            Loader=yaml.BaseLoader,
        )
        self.steps = workflow['jobs']['sonarcloud']['steps']
        self.validation = next(step for step in self.steps if step.get('id') == 'project')
        self.scan = next(step for step in self.steps
                         if step.get('uses', '').startswith('SonarSource/sonarqube-scan-action@'))

    def run_step(self, step, **env):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / 'output'
            result = subprocess.run(
                ['bash', '--noprofile', '--norc', '-e', '-o', 'pipefail', '-c', step['run']],
                cwd=temp,
                env={**os.environ, **step.get('env', {}), **env, 'GITHUB_OUTPUT': str(output)},
                capture_output=True,
                text=True,
            )
            return result, output.read_text() if output.exists() else ''

    def test_valid_project_keys_are_preserved(self):
        for key in ('storytold_pdfcraft', 'storytold_craft-common-actions', 'Org:app_1.2-3'):
            with self.subTest(key=key):
                result, output = self.run_step(self.validation, PROJECT_KEY=key)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(output, f'key={key}\n')

    def test_invalid_keys_cannot_publish_scanner_arguments(self):
        for key in (
            '', '-Dsonar.host.url=https://attacker.invalid',
            '-leading-dash', 'key -Dsonar.host.url=https://attacker.invalid',
            'key\t-Dsonar.host.url=https://attacker.invalid',
            'key\n-Dsonar.host.url=https://attacker.invalid',
            'key\r\nkey=other', 'key" -Dsonar.host.url="https://attacker.invalid',
            "key' -Dsonar.host.url='https://attacker.invalid",
            'key\\ value', '$(touch injected)', '`touch injected`', 'key/other',
            'a' * 401,
        ):
            with self.subTest(key=key):
                result, output = self.run_step(self.validation, PROJECT_KEY=key)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('::error::', result.stdout)
                self.assertEqual(output, '')

    def test_scan_consumes_only_the_validated_key(self):
        self.assertEqual(self.validation['shell'], 'bash')
        self.assertEqual(
            self.validation['env']['PROJECT_KEY'],
            "${{ inputs.project-key != '' && inputs.project-key || "
            "format('storytold_{0}', github.event.repository.name) }}",
        )
        self.assertLess(self.steps.index(self.validation), self.steps.index(self.scan))
        self.assertEqual(self.validation['if'], "steps.token.outputs.available == 'true'")
        self.assertEqual(self.scan['if'], self.validation['if'])
        self.assertEqual(self.scan['env']['SONAR_HOST_URL'], 'https://sonarcloud.io')
        self.assertEqual(self.scan['env']['SONAR_TOKEN'], '${{ secrets.SONAR_TOKEN }}')
        self.assertEqual(
            self.scan['with']['args'].splitlines()[0],
            '"-Dsonar.projectKey=${{ steps.project.outputs.key }}" -Dsonar.organization=storytold '
            '-Dsonar.rust.lcov.reportPaths=lcov.info',
        )

    def test_coverage_is_generated_before_the_scan_imports_it(self):
        coverage = next(step for step in self.steps if step.get('id') == 'coverage')
        self.assertLess(self.steps.index(coverage), self.steps.index(self.scan))
        self.assertIn('cargo llvm-cov', coverage['run'])
        self.assertIn('--output-path lcov.info', coverage['run'])
        self.assertEqual(coverage['continue-on-error'], 'true')

    def test_sharding_defaults_to_four_and_keeps_the_unsharded_path(self):
        workflow = yaml.load(
            (ROOT / '.github/workflows/sonarcloud.yml').read_text(), Loader=yaml.BaseLoader)
        shards = workflow['on']['workflow_call']['inputs']['shards']
        self.assertEqual(shards['default'], '4')
        coverage = next(step for step in self.steps if step.get('id') == 'coverage')
        self.assertIn('inputs.shards <= 1', coverage['if'])
        self.assertEqual(workflow['jobs']['plan']['if'], 'inputs.shards > 1')
        self.assertEqual(workflow['jobs']['coverage']['strategy']['fail-fast'], 'false')

    def test_shards_run_partitions_of_one_prebuilt_archive(self):
        workflow = yaml.load(
            (ROOT / '.github/workflows/sonarcloud.yml').read_text(), Loader=yaml.BaseLoader)
        build = workflow['jobs']['build']['steps']
        self.assertTrue(any('nextest archive --workspace' in step.get('run', '') for step in build))
        tests = next(step for step in workflow['jobs']['coverage']['steps']
                     if step.get('name') == "Run this shard's tests")
        self.assertIn('--partition count:${{ matrix.shard }}/${{ inputs.shards }}', tests['run'])
        self.assertIn('--archive-file archive/nextest-archive.tar.zst', tests['run'])
        self.assertEqual(workflow['jobs']['coverage']['needs'], ['plan', 'build'])

    def test_sharded_scan_steps_only_run_when_the_plan_runs(self):
        workflow = yaml.load(
            (ROOT / '.github/workflows/sonarcloud.yml').read_text(), Loader=yaml.BaseLoader)
        sharded = [step for step in workflow['jobs']['sonarcloud']['steps']
                   if 'inputs.shards > 1' in step.get('if', '')]
        self.assertGreaterEqual(len(sharded), 4)
        for step in sharded:
            with self.subTest(step=step.get('name')):
                self.assertIn("hashFiles('Cargo.toml') != ''", step['if'])

    def test_shard_count_is_a_whole_number_within_the_matrix_limit(self):
        workflow = yaml.load(
            (ROOT / '.github/workflows/sonarcloud.yml').read_text(), Loader=yaml.BaseLoader)
        plan = next(step for step in workflow['jobs']['plan']['steps'] if step.get('id') == 'matrix')
        result, output = self.run_step(plan, SHARDS='2.5', SONAR_TOKEN='t')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('whole number', result.stdout)
        result, output = self.run_step(plan, SHARDS='257', SONAR_TOKEN='t')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('256', result.stdout)
        result, output = self.run_step(plan, SHARDS='3', SONAR_TOKEN='t')
        # The step runs without a Cargo.toml, so there is no workspace to shard.
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output, 'matrix={"shard":[]}\n')

    def test_merge_uses_the_archives_toolchain_and_test_binaries_only(self):
        workflow = yaml.load(
            (ROOT / '.github/workflows/sonarcloud.yml').read_text(), Loader=yaml.BaseLoader)
        steps = workflow['jobs']['sonarcloud']['steps']
        combine = next(step for step in steps if step.get('name') == 'Combine shard coverage')
        self.assertIn('rustc-release.txt', combine['run'] + ' '.join(
            step.get('run', '') for step in steps))
        self.assertIn("select(.\"build-platform\" == \"target\")", combine['run'])
        self.assertNotIn('-executable', combine['run'])
        build = workflow['jobs']['build']['steps']
        self.assertTrue(any('rustc-release.txt' in step.get('with', {}).get('path', '')
                            for step in build))

    def test_shard_masks_only_test_failures(self):
        workflow = yaml.load(
            (ROOT / '.github/workflows/sonarcloud.yml').read_text(), Loader=yaml.BaseLoader)
        tests = next(step for step in workflow['jobs']['coverage']['steps']
                     if step.get('name') == "Run this shard's tests")
        self.assertNotIn('continue-on-error', tests)
        self.assertIn('-ne 100', tests['run'])
        self.assertIn('no coverage profiles', tests['run'])

    def test_scan_fails_when_a_prerequisite_job_failed(self):
        workflow = yaml.load(
            (ROOT / '.github/workflows/sonarcloud.yml').read_text(), Loader=yaml.BaseLoader)
        condition = workflow['jobs']['sonarcloud']['if']
        for job in ('plan', 'build', 'coverage'):
            self.assertIn(f'needs.{job}.result', condition)
        self.assertIn('"success","skipped"', condition)
        self.assertIn('!cancelled()', condition)

    def test_token_availability_still_controls_the_scan(self):
        token_step = next(step for step in self.steps if step.get('id') == 'token')
        for token, available in (('', 'false'), ('synthetic-token', 'true')):
            with self.subTest(available=available):
                result, output = self.run_step(token_step, SONAR_TOKEN=token)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(output, f'available={available}\n')
                self.assertNotIn('synthetic-token', result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
