"""Regression tests for benchmark breadth, evaluation cost and failure artifacts."""

import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
import subprocess
from unittest.mock import patch

from kernel.engine import Engine, SOURCE
from kernel.evaluation import tasks, repair_case


class ImprovementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def engine(self, **kwargs):
        return Engine(self.root / 'state',
                      [sys.executable, '-B', str(SOURCE / 'examples/demo_coder.py')], **kwargs)

    def test_advanced_benchmark_has_six_families_and_is_deterministic(self):
        cases = tasks(10, suite='advanced')
        self.assertEqual(cases, tasks(10, suite='advanced'))
        self.assertNotEqual(cases, tasks(11, suite='advanced'))
        self.assertEqual(len({task['operation'] for task, _ in cases}), 6)
        for task, expected in cases:
            if task['operation'] not in ('filtered_sum', 'weighted_mean', 'group_max_total'):
                continue
            text = task['observation'].replace('Tool result:\n', '').replace('```json\n', '').replace('\n```', '')
            rows = json.loads(text)['rows']
            if task['operation'] == 'filtered_sum':
                answer = sum(r['value'] for r in rows if r['value'] > 10)
            elif task['operation'] == 'weighted_mean':
                answer = sum(r['value'] * r['weight'] for r in rows) / sum(r['weight'] for r in rows)
            else:
                groups = {}
                for row in rows:
                    groups[row['group']] = groups.get(row['group'], 0) + row['value']
                answer = max(groups.values())
            self.assertEqual(answer, expected)

    def test_suite_is_persisted_and_cannot_change_mid_experiment(self):
        engine = self.engine(benchmark='advanced')
        engine.initialize()
        self.assertEqual(Engine(engine.root).benchmark, 'advanced')
        with self.assertRaisesRegex(ValueError, 'benchmark changed'):
            Engine(engine.root, benchmark='basic')

    def test_advanced_repair_cases_include_multiple_files(self):
        cases = [repair_case('advanced', i) for i in range(3)]
        self.assertEqual(len({case['name'] for case in cases}), 3)
        self.assertIn('helpers.py', cases[2]['mutable'])
        self.assertNotIn('test_solution.py', cases[2]['mutable'])

    def test_advanced_initial_solver_has_improvement_headroom(self):
        engine = self.engine(benchmark='advanced')
        engine.initialize()
        result = engine.score(engine.version(engine.state()['active']), 10)
        self.assertLess(result['score'], 1)
        self.assertEqual(len(result['traces']), 12)

    def test_correct_multifile_repair_scores_full_marks(self):
        engine = self.engine(benchmark='advanced')
        engine.initialize()
        engine.command = [sys.executable, '-c',
                          "from pathlib import Path; "
                          "Path('helpers.py').write_text('def normalize(v): return v.strip().lower()\\n'); "
                          "Path('solution.py').write_text('from helpers import normalize\\ndef unique_active(rows): return len({normalize(r[\"email\"]) for r in rows if r[\"active\"] == True})\\n')"]
        audit = self.root / 'audit'
        audit.mkdir()
        self.assertEqual(engine.meta_probe(engine.version(engine.state()['active']), audit, 'test', 2), 1)

    def test_baseline_cache_resumes_without_repeating_paid_probe(self):
        engine = self.engine(cache_baseline=True, trials=1)
        engine.initialize()
        version = engine.version(engine.state()['active'])
        first, second = self.root / 'first', self.root / 'second'
        first.mkdir()
        second.mkdir()
        rows = engine.trials(version, first, 'base-meta', 10)
        restart = self.engine(cache_baseline=True, trials=1)
        with patch.object(restart, 'meta_probe', side_effect=AssertionError('probe was repeated')):
            self.assertEqual(restart.trials(version, second, 'base-meta', 10), rows)
        self.assertTrue(json.loads((second / 'base-meta-0.cache.json').read_text())['hit'])
        self.assertEqual(json.loads((engine.root / 'usage.json').read_text())['calls'], 1)

    def test_confirmation_never_uses_baseline_cache(self):
        engine = self.engine(cache_baseline=True, trials=1)
        engine.initialize()
        version = engine.version(engine.state()['active'])
        audit = self.root / 'audit'
        audit.mkdir()
        engine.trials(version, audit, 'base-meta', 10)
        engine.trials(version, audit, 'base-confirm-meta', 10010)
        self.assertEqual(json.loads((engine.root / 'usage.json').read_text())['calls'], 2)

    def test_cache_invalidates_on_coder_configuration_change(self):
        engine = self.engine(cache_baseline=True, trials=1)
        engine.initialize()
        version = engine.version(engine.state()['active'])
        audit = self.root / 'audit'
        audit.mkdir()
        engine.trials(version, audit, 'base-meta', 10)
        engine.command = [sys.executable, '-c', "print('changed configuration')"]
        engine.trials(version, audit, 'base-meta', 10)
        self.assertEqual(json.loads((engine.root / 'usage.json').read_text())['calls'], 2)

    def test_evidence_is_not_duplicated_in_proposal(self):
        engine = self.engine(trials=1)
        with contextlib.redirect_stdout(io.StringIO()):
            record = engine.run(1)[0]
        prompt = (Path(record['audit']) / 'proposal.txt').read_text()
        self.assertEqual(prompt.count('Evidence:\n'), 1)

    def test_broken_python_is_rejected_before_model_review(self):
        engine = self.engine()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'agent').mkdir()
            (root / 'evolution').mkdir()
            (root / 'agent/solver.py').write_text('this is invalid !')
            (root / 'evolution/policy.py').write_text('pass')
            with self.assertRaisesRegex(ValueError, 'invalid Python'):
                engine.validate_tree(root)

    def test_budget_overrun_preserves_candidate_before_cleanup(self):
        engine = self.engine(trials=1, max_tokens=5)
        coder = self.root / 'overrun.py'
        coder.write_text("import json\nfrom pathlib import Path\n"
                         "p=Path('agent/solver.py')\n"
                         "if p.exists():\n"
                         "    p.write_text(p.read_text()+'\\n# paid patch before overrun\\n')\n"
                         "    usage=10\n"
                         "else: usage=0\n"
                         "print(json.dumps({'type':'usage','input_tokens':usage,'output_tokens':0}))\n")
        engine.command = [sys.executable, str(coder)]
        before = engine.initialize()['active']
        with contextlib.redirect_stdout(io.StringIO()):
            record = engine.run(1)[0]
        self.assertFalse(record['accepted'])
        self.assertIn('budget exceeded', record['reason'])
        self.assertEqual(record['stage'], 'coding')
        audit = Path(record['audit'])
        self.assertIn('paid patch before overrun', (audit / 'rejected-source/agent/solver.py').read_text())
        self.assertIn('paid patch before overrun', (audit / 'rejected-changes.patch').read_text())
        self.assertTrue(record['archive']['complete'])
        self.assertEqual(engine.state()['active'], before)

    def test_failed_candidate_archive_skips_symlinks_and_limits_size(self):
        engine = self.engine()
        current, candidate, audit = [self.root / name for name in ('current', 'candidate', 'audit')]
        current.mkdir()
        (candidate / 'agent').mkdir(parents=True)
        audit.mkdir()
        secret = self.root / 'secret'
        secret.write_text('do not archive this')
        (candidate / 'agent/link.py').symlink_to(secret)
        (candidate / 'agent/huge.py').write_bytes(b'x' * 200001)
        metadata = engine.archive_failure(candidate, current, audit)
        self.assertFalse(metadata['complete'])
        self.assertEqual(len(metadata['skipped']), 2)
        self.assertFalse((audit / 'rejected-source/agent/link.py').exists())
        self.assertNotIn('do not archive this', (audit / 'rejected-changes.patch').read_text())

    def test_timeout_after_writing_preserves_partial_patch(self):
        engine = self.engine(trials=1)
        engine.initialize()
        coder = self.root / 'timeout.py'
        coder.write_text("import time\nfrom pathlib import Path\n"
                         "p=Path('agent/solver.py')\n"
                         "if p.exists():\n"
                         "    p.write_text(p.read_text()+'\\n# partial patch\\n')\n"
                         "    time.sleep(10)\n")
        engine.command = [sys.executable, str(coder)]
        engine.timeout = 1
        with contextlib.redirect_stdout(io.StringIO()):
            record = engine.run(1)[0]
        self.assertFalse(record['accepted'])
        self.assertEqual(record['stage'], 'coding')
        self.assertIn('partial patch', (Path(record['audit']) / 'rejected-source/agent/solver.py').read_text())

    def test_reviewer_rejection_also_has_failure_archive(self):
        engine = self.engine(trials=1, reviewer=[sys.executable, '-c',
                             "print('{\"approved\":false,\"reason\":\"test veto\"}')"])
        with contextlib.redirect_stdout(io.StringIO()):
            record = engine.run(1)[0]
        self.assertEqual(record['stage'], 'review')
        self.assertTrue((Path(record['audit']) / 'rejected-source/agent/solver.py').is_file())
