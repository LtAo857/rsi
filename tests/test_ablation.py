import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from kernel.ablation import apply_spec, usage_delta, validate_specs
from kernel.engine import Engine, SOURCE


SPEC = {'name':'disable_sum','path':'agent/solver.py','before':'return sum(values)','after':'return 0'}


class AblationTests(unittest.TestCase):
    def test_scope_unique_names_and_shortening(self):
        validate_specs([SPEC])
        for specs in ([], [SPEC]*2, [{**SPEC,'path':'kernel/engine.py'}],
                      [{**SPEC,'path':'agent/../../outside.py'}], [{**SPEC,'after':SPEC['before']}],
                      [{**SPEC,'before':''}], [{**SPEC,'name':'../bad'}]):
            with self.subTest(specs=specs), self.assertRaises(ValueError):
                validate_specs(specs)

    def test_literal_requires_exactly_one_match(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'agent').mkdir()
            file = root/'agent/solver.py'
            for text in ('no match', SPEC['before']*2):
                file.write_text(text)
                with self.assertRaisesRegex(ValueError, 'exactly once'):
                    apply_spec(root,SPEC)
                self.assertEqual(file.read_text(),text)

    def test_paired_trials_detect_regression_without_publishing(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = Engine(directory)
            engine.initialize()
            state = engine.state()
            history = (engine.root/'history.jsonl').read_bytes()
            source = (engine.version(state['active'])/'agent/solver.py').read_bytes()
            with engine.lock():
                # Health requires sum([2,3]) == 5; disable mean instead.
                spec = {**SPEC,'name':'disable_mean','before':'return sum(values) / len(values)','after':'return 0'}
                result = engine.ablate([spec],2)
            variant = result['variants'][0]
            self.assertEqual(variant['conclusion'],'regression_observed')
            self.assertGreater(variant['observed_regressions'],0)
            self.assertEqual(variant['trials'][0]['order'],['baseline','ablated'])
            self.assertEqual(variant['trials'][1]['order'],['ablated','baseline'])
            self.assertEqual(engine.state(),state)
            self.assertEqual((engine.root/'history.jsonl').read_bytes(),history)
            self.assertEqual((engine.version(state['active'])/'agent/solver.py').read_bytes(),source)

    def test_all_variants_preflight_before_any_scoring(self):
        class NoScore(Engine):
            def score(self,*args,**kwargs):
                raise AssertionError('must not score')
        with tempfile.TemporaryDirectory() as directory:
            engine = NoScore(directory)
            engine.initialize()
            first = {**SPEC,'name':'doc','before':'A deliberately small starting agent','after':'Agent'}
            bad = {**SPEC,'name':'missing','before':'not in source'}
            with self.assertRaises(ValueError):
                engine.ablate([first,bad])
            report = json.loads(next((engine.root/'ablations').glob('*/result.json')).read_text())
            self.assertEqual(report['status'],'failed')
            self.assertFalse((engine.root/'ablation.json').exists())

    def test_unknown_usage_is_not_fabricated_as_zero(self):
        self.assertEqual(usage_delta({}, {'calls':1,'unknown_tokens':1})['tokens'],None)
        self.assertEqual(usage_delta({'tokens':10},{'tokens':16})['tokens'],6)

    def test_cli_no_regression_is_not_a_deletion_recommendation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = {**SPEC,'name':'doc','before':'A deliberately small starting agent','after':'Agent'}
            (root/'spec.json').write_text(json.dumps([spec]))
            process = subprocess.run([sys.executable,'-m','kernel','--state',str(root/'state'),
                                      'ablate',str(root/'spec.json'),'--seeds','1'],cwd=SOURCE,
                                     capture_output=True,text=True)
            self.assertEqual(process.returncode,0,process.stderr)
            result = json.loads(process.stdout)
            self.assertEqual(result['variants'][0]['conclusion'],'no_regression_observed_not_proof_of_redundancy')
            self.assertEqual(result['status'],'complete')

    def test_budget_exhaustion_keeps_partial_report(self):
        model = [sys.executable,str(SOURCE/'tests/fixtures/datadesk_model.py')]
        with tempfile.TemporaryDirectory() as directory:
            engine = Engine(directory,task_command=model,benchmark='datadesk',max_calls=1)
            engine.initialize()
            state = engine.state()
            spec = {**SPEC,'name':'doc','before':'A deliberately small starting agent','after':'Agent'}
            with self.assertRaises(RuntimeError):
                engine.ablate([spec],1)
            report = json.loads(next((engine.root/'ablations').glob('*/result.json')).read_text())
            self.assertEqual(report['status'],'failed')
            self.assertEqual(engine.state(),state)
            self.assertEqual(json.loads((engine.root/'usage.json').read_text())['calls'],1)

    def test_disabling_fence_parser_exposes_datadesk_regression(self):
        from tests.fixtures.datadesk_model import query_for
        class FencedFixture(Engine):
            def model_response(self, root, messages):
                task = json.loads(messages['prompt'])
                history = task['history']
                if not history:
                    action = {'tool':'schema','args':{}}
                elif 'rows' in history[-1]['result']:
                    action = {'answer':history[-1]['result']['rows'][0][0]}
                else:
                    action = {'tool':'sql','args':{'query':query_for(task['question'])}}
                return '```json\n'+json.dumps(action)+'\n```'
        with tempfile.TemporaryDirectory() as directory:
            engine = FencedFixture(directory,task_command=['offline-fixture'],benchmark='datadesk')
            engine.initialize()
            spec = {**SPEC, 'name':'no_fence_parser',
                    'before':"    if text.startswith('```'):\n        text = text.split('\\n', 1)[-1].rsplit('```', 1)[0].strip()\n",'after':''}
            result = engine.ablate([spec],1)
            trial = result['variants'][0]['trials'][0]
            self.assertEqual(trial['baseline']['score'],1)
            self.assertEqual(trial['ablated']['score'],0)
            self.assertEqual(trial['regressed'],5)
