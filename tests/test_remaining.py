import copy
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from kernel.ablation import assess_pairs
from kernel.backends import backend_command
from kernel.diagnostics import compact_evidence, diagnose_trace, task_failure_evidence
from kernel.engine import Engine, SOURCE
from kernel.semantic import validate_review


class RemainingTests(unittest.TestCase):
    def test_later_trial_failures_are_not_hidden_and_compaction_is_explicit(self):
        first={'score':1,'traces':[{'ok':True,'task':{'id':'x'}}]}
        later={'score':0,'traces':[{'ok':False,'task':{'id':'x'},'history':[
            {'action':{'tool':'read_csv','args':{}},'result':{'rows':[{'qty':i} for i in range(20)]}}]}]}
        failures=task_failure_evidence([first,later])
        self.assertEqual(failures[0]['id'],'trial-1-task-0')
        result=failures[0]['trace']['history'][0]['result']
        self.assertEqual(result['evidence_rows_omitted'],18)
        self.assertEqual(len(later['traces'][0]['history'][0]['result']['rows']),20)

    def test_fixed_arithmetic_proof_requires_complete_data(self):
        trace = {'ok':False,'task':{'id':'p-active_sum'},'answer':'99','stop_reason':'answer',
                 'history':[{'action':{'tool':'read_csv','args':{'path':'records.csv'}},
                             'result':{'rows':[{'status':'active','qty':'3'}],'next_offset':None}}]}
        kinds = {f['kind'] for f in diagnose_trace(trace)['findings']}
        self.assertIn('verified_arithmetic_mismatch',kinds)
        trace['history'][0]['result']['next_offset']=20
        self.assertNotIn('verified_arithmetic_mismatch',{f['kind'] for f in diagnose_trace(trace)['findings']})

    def test_semantic_hypotheses_are_bounded_and_not_grades(self):
        trace={'history':[{'action':{'tool':'sql'},'result':{}}]}
        text=json.dumps({'findings':[{'kind':'sql_logic','steps':[0],'hypothesis':'Check filter condition'}]})
        result=validate_review(text,trace)
        self.assertEqual(result['findings'][0]['status'],'unverified_hypothesis')
        for steps in ([],[True],[1],[-1]):
            with self.subTest(steps=steps),self.assertRaises(ValueError):
                validate_review(json.dumps({'findings':[{'kind':'sql_logic','steps':steps,'hypothesis':'test'}]}),trace)
        with self.assertRaisesRegex(ValueError,'actual SQL action'):
            validate_review(text,{'history':[{'action':{'tool':'read_csv'}}]})

    def test_semantic_review_only_failed_tasks_and_preserves_score(self):
        class Reviewer(Engine):
            def model_response(self,root,messages,command=None):
                self.calls+=1
                return json.dumps({'findings':[{'kind':'arithmetic','steps':[0],'hypothesis':'Recompute total'}]})
        with tempfile.TemporaryDirectory() as directory:
            e=Reviewer(directory,semantic_reviewer=['fixture']);e.initialize();e.calls=0
            evaluation={'score':.5,'traces':[{'ok':False,'history':[{}]},{'ok':True}]}
            result=e.semantic_review(e.version(e.state()['active']),evaluation)
            self.assertEqual(e.calls,1)
            self.assertEqual(result['score'],.5)
            self.assertNotIn('semantic_review',result['traces'][1])

    def test_ablation_requires_repeated_paired_benefit_and_no_regressions(self):
        pair={'regressed':0,'baseline':{'score':1,'tokens':100},'ablated':{'score':1,'tokens':70}}
        self.assertTrue(assess_pairs([pair,pair]))
        self.assertFalse(assess_pairs([pair]))
        bad=copy.deepcopy(pair);bad['regressed']=1
        self.assertFalse(assess_pairs([pair,bad]))
        bad=copy.deepcopy(pair);bad['ablated']['tokens']=None
        self.assertFalse(assess_pairs([pair,bad]))

    def test_confirmation_uses_fresh_seeds_and_never_authorizes_deletion(self):
        class Cheap(Engine):
            def score(self,root,seed,heldout=False):
                self.seeds.append(seed)
                return {'score':1,'traces':[{'task':{'seed':seed},'ok':True}]}
        with tempfile.TemporaryDirectory() as directory:
            e=Cheap(directory);e.initialize();e.seeds=[]
            spec={'name':'doc','path':'agent/solver.py','before':'A deliberately small starting agent','after':'Agent'}
            # Test score-gain gate independently of the offline zero-cost fixture.
            with patch('kernel.engine.assess_pairs',return_value=True):
                report=e.ablate([spec],2,100,2)
            self.assertEqual(e.seeds,[100,100,101,101,10100,10100,10101,10101])
            self.assertTrue(report['variants'][0]['confirmed_task_benefit'])
            self.assertFalse(report['variants'][0]['deletion_authorized'])

    def test_confirmation_regression_is_in_final_conclusion(self):
        class Cheap(Engine):
            def score(self,root,seed,heldout=False):
                variant=root.name=='doc'
                path=self.root/'usage.json'
                usage=json.loads(path.read_text()) if path.exists() else {'calls':0,'tokens':0}
                usage['calls']+=1;usage['tokens']+=70 if variant else 100
                path.write_text(json.dumps(usage))
                ok=not (variant and seed==10101)
                return {'score':int(ok),'traces':[{'task':{'seed':seed},'ok':ok}]}
        with tempfile.TemporaryDirectory() as directory:
            e=Cheap(directory);e.initialize()
            spec={'name':'doc','path':'agent/solver.py','before':'A deliberately small starting agent','after':'Agent'}
            variant=e.ablate([spec],2,100,2)['variants'][0]
            self.assertEqual(len(variant['confirmation']),2)
            self.assertEqual(variant['observed_regressions'],1)
            self.assertEqual(variant['conclusion'],'regression_observed')
            self.assertFalse(variant['confirmed_task_benefit'])
            self.assertFalse(variant['deletion_authorized'])


@unittest.skipUnless(sys.platform=='darwin' and Path('/usr/bin/sandbox-exec').exists(),'macOS Seatbelt required')
class MacIsolationTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.root=Path(self.directory.name)
        self.engine=Engine(self.root/'state',host_sandbox='macos');self.engine.initialize()
        self.candidate=self.root/'candidate'
        shutil.copytree(self.engine.version(self.engine.state()['active']),self.candidate)
        self.secret=self.root/'secret.txt';self.secret.write_text('test-only-secret')

    def run_source(self,body):
        (self.candidate/'agent/solver.py').write_text('def solve(task):\n'+''.join('    '+line+'\n' for line in body.splitlines()))
        return self.engine.call(self.candidate,'solve',task={})

    def test_outside_reads_and_writes_are_denied(self):
        for operation in ('read_text()',"write_text('modified')"):
            with self.subTest(operation=operation),self.assertRaises(RuntimeError):
                self.run_source('from pathlib import Path\nreturn Path('+repr(str(self.secret))+').'+operation)
            self.assertEqual(self.secret.read_text(),'test-only-secret')

    def test_network_is_denied_and_environment_has_no_credentials(self):
        with socket.socket() as server:
            server.bind(('127.0.0.1',0));server.listen()
            with self.assertRaisesRegex(RuntimeError,'PermissionError|Operation not permitted'):
                self.run_source('import socket\nsocket.create_connection(("127.0.0.1",'+str(server.getsockname()[1])+'))\nreturn 1')
        with patch.dict('os.environ',{'RSI_TEST_SECRET':'must-not-leak'}):
            self.assertIsNone(self.run_source('import os\nreturn os.environ.get("RSI_TEST_SECRET")'))

    def test_children_inherit_policy(self):
        body=('import subprocess,sys\nr=subprocess.run([sys.executable,"-I","-c",'+
              repr('from pathlib import Path; print(Path('+repr(str(self.secret))+').read_text())')+
              '],capture_output=True,text=True)\nreturn {"code":r.returncode,"error":r.stderr}')
        result=self.run_source(body)
        self.assertNotEqual(result['code'],0)
        self.assertIn('PermissionError',result['error'])

    def test_process_introspection_is_denied(self):
        with self.assertRaisesRegex(RuntimeError,'PermissionError|Operation not permitted'):
            self.run_source('import os\nreturn list(os.uname())')

    def test_source_writes_are_denied_and_mode_is_persisted(self):
        with self.assertRaises(RuntimeError):
            self.run_source('from pathlib import Path\nPath("agent/new.py").write_text("changed")\nreturn 1')
        self.assertEqual(Engine(self.root/'state').host_sandbox,'macos')
        with self.assertRaises(ValueError):
            Engine(self.root/'state',host_sandbox='none')

    def test_custom_networked_coders_are_refused(self):
        with self.assertRaises(ValueError):
            self.engine.code(self.candidate,'test',self.root/'log',command=['sh','-c','echo unsafe'])
        self.assertFalse((self.engine.root/'usage.json').exists())

    def test_network_adapter_ignores_pythonpath_injection(self):
        poison=self.root/'poison';poison.mkdir()
        marker=self.root/'imported'
        (poison/'json.py').write_text('from pathlib import Path\nPath('+repr(str(marker))+').touch()\nraise RuntimeError("injected")\n')
        command=backend_command('deepseek','task','fixture',api_key_env='RSI_MISSING_SANDBOX_TEST_KEY')
        with patch.dict('os.environ',{'PYTHONPATH':str(poison)}):
            with self.assertRaises(RuntimeError):
                self.engine.code(self.candidate,json.dumps({'system':'test','prompt':'test'}),self.root/'adapter.log',command)
        self.assertFalse(marker.exists())
        self.assertIn('missing API key', (self.root/'adapter.log').read_text())

    def test_stale_unsandboxed_instance_cannot_reuse_new_sandbox_state(self):
        path=self.root/'race'
        stale=Engine(path)
        secure=Engine(path,host_sandbox='macos');secure.initialize()
        with self.assertRaisesRegex(ValueError,'configuration changed'):
            stale.initialize()

    def test_repair_probe_executes_in_sandbox(self):
        class FixedPatch(Engine):
            def code(self,workspace,prompt,log,command=None):
                (workspace/'solution.py').write_text('def total(values):\n    return sum(values)\n')
        e=FixedPatch(self.root/'repair',host_sandbox='macos');e.initialize()
        self.assertEqual(e.meta_probe(e.version(e.state()['active']),self.root,'repair'),1)
