"""Offline provider contract tests: HTTP server and fake executable, no model calls."""
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
import shlex
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from kernel.engine import Engine, SOURCE


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.requests = []
        self.status = 200
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                owner.requests.append((self.path, self.headers.get('Authorization'), payload))
                body = {'choices':[{'message':{'content':owner.reply}}],
                        'usage':{'prompt_tokens':12, 'completion_tokens':3}}
                self.send_response(owner.status)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps(body).encode())

            def log_message(self, *args):
                pass

        self.reply = '5'
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.url = f'http://127.0.0.1:{server.server_port}/v1'
        self.env = patch.dict(os.environ, {'RSI_TEST_KEY':'test-only-secret'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def command(self, role='task'):
        return [sys.executable, '-B', str(SOURCE / 'kernel/backends.py'), 'deepseek',
                '--role', role, '--model', 'test-model', '--base-url', self.url,
                '--api-key-env', 'RSI_TEST_KEY']

    def test_deepseek_task_persists_configuration_and_accounts_usage(self):
        engine = Engine(self.root / 'state', task_command=self.command())
        self.assertEqual(engine.solve({'operation':'sum','observation':'{"values":[2,3]}'}), 5)
        self.assertEqual(len(self.requests), 1)  # health checks never spend model tokens
        path, authorization, payload = self.requests[0]
        self.assertEqual(path, '/v1/chat/completions')
        self.assertEqual(authorization, 'Bearer test-only-secret')
        self.assertEqual(payload['model'], 'test-model')
        usage = json.loads((engine.root / 'usage.json').read_text())
        self.assertEqual(usage['tokens'], 15)
        replacement = Engine(engine.root)
        self.assertEqual(replacement.solve({'operation':'sum','observation':'{"values":[2,3]}'}), 5)
        self.assertEqual(len(self.requests), 2)
        for path in engine.root.rglob('*.json'):
            self.assertNotIn('test-only-secret', path.read_text())

    def test_task_outage_does_not_become_a_zero_score(self):
        engine = Engine(self.root / 'state', task_command=self.command())
        engine.initialize()
        self.status = 429
        with self.assertRaisesRegex(RuntimeError, 'model unavailable'):
            engine.score(engine.version(engine.state()['active']), 10)
        self.assertEqual(len(self.requests), 1)

    def test_deepseek_coder_applies_manifest_and_rejects_escaping_paths(self):
        from kernel.backends import apply_patch_response
        candidate = self.root / 'candidate'
        (candidate / 'agent').mkdir(parents=True)
        apply_patch_response(candidate, {'files':{'agent/solver.py':'x = 1\n'},
                                         'proposal':{'analysis':'test','changes':[]}})
        self.assertEqual((candidate / 'agent/solver.py').read_text(), 'x = 1\n')
        self.assertTrue((candidate / '.rsi-proposal.json').exists())
        with self.assertRaisesRegex(ValueError, 'scope'):
            apply_patch_response(candidate, {'files':{'../escaped.py':'bad'}})
        self.assertFalse((self.root / 'escaped.py').exists())

    def test_deepseek_coder_http_writes_only_structured_patch(self):
        candidate = self.root / 'candidate'
        (candidate / 'agent').mkdir(parents=True)
        (candidate / 'agent/solver.py').write_text('old = True\n')
        self.reply = json.dumps({'files':{'agent/solver.py':'new = True\n'},
                                 'proposal':{'analysis':'test','changes':[]}})
        result = subprocess.run(self.command('coder'), input='Apply an evidence-linked patch',
                                text=True, capture_output=True, cwd=candidate)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((candidate / 'agent/solver.py').read_text(), 'new = True\n')
        self.assertTrue((candidate / '.rsi-proposal.json').exists())
        self.assertEqual(json.loads(result.stdout.splitlines()[-1])['output_tokens'], 3)

    def test_cli_task_budget_and_persisted_backend(self):
        state = self.root / 'cli-state'
        task = json.dumps({'operation':'sum','observation':'{"values":[2,3]}'})
        command = [sys.executable, '-m', 'kernel', '--state', str(state), 'solve',
                   task, '--max-calls', '1']
        first = subprocess.run(command + ['--task-backend','deepseek','--task-model','test-model',
                                          '--base-url',self.url,'--api-key-env','RSI_TEST_KEY'],
                               text=True, capture_output=True, cwd=SOURCE)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(json.loads(first.stdout), 5)
        second = subprocess.run(command, text=True, capture_output=True, cwd=SOURCE)
        self.assertNotEqual(second.returncode, 0)
        self.assertIn('budget', second.stderr)
        self.assertEqual(len(self.requests), 1)

    def test_deepseek_review_emits_verdict_and_native_usage(self):
        self.reply = '{"approved":false,"reason":"missing evidence"}'
        result = subprocess.run(self.command('reviewer'), input='Review this patch',
                                text=True, capture_output=True, cwd=self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertFalse(rows[0]['approved'])
        self.assertEqual(rows[-1]['input_tokens'], 12)
        self.assertEqual(rows[-1]['output_tokens'], 3)
        self.assertNotIn('test-only-secret', result.stdout + result.stderr)

    def test_codex_child_inherits_system_proxy_without_changing_parent(self):
        from kernel.backends import codex_completion
        fake = self.root / 'codex'
        fake.write_text('#!' + sys.executable + '\n' + """import os,sys
from pathlib import Path
assert os.environ.get('HTTPS_PROXY') == 'http://127.0.0.1:7897'
assert os.environ.get('HTTP_PROXY') == 'http://127.0.0.1:7897'
assert os.environ.get('ALL_PROXY') == 'http://127.0.0.1:7897'
assert os.environ.get('NO_PROXY') == 'localhost,127.0.0.1'
Path(sys.argv[sys.argv.index('-o')+1]).write_text('5')
""")
        fake.chmod(0o755)
        with patch.dict(os.environ, {'PATH':str(self.root)}, clear=True):
            with patch('urllib.request.getproxies', return_value={
                'http':'http://127.0.0.1:7897', 'https':'http://127.0.0.1:7897',
                'no':'localhost,127.0.0.1'}):
                answer, _ = codex_completion('Return 5', timeout=10)
            self.assertNotIn('HTTPS_PROXY', os.environ)
        self.assertEqual(answer, '5')

    def test_codex_isolation_is_forwarded_to_child(self):
        from kernel.backends import codex_completion
        fake = self.root / 'codex'
        fake.write_text('#!' + sys.executable + '\n' + """import sys
from pathlib import Path
assert '--ignore-user-config' in sys.argv
Path(sys.argv[sys.argv.index('-o')+1]).write_text('5')
""")
        fake.chmod(0o755)
        with patch.dict(os.environ, {'PATH':str(self.root) + os.pathsep + os.environ['PATH']}):
            text, _ = codex_completion('Return 5', timeout=10, isolated=True)
        self.assertEqual(text, '5')

    def test_codex_timeout_preserves_already_reported_usage(self):
        from kernel.backends import ModelUnavailable, codex_completion
        from kernel.search import inspect_trace
        # TimeoutExpired exposes bytes even when the process used text=True.
        # Inject this boundary result so process startup speed cannot erase the fixture.
        output = b'{"type":"turn.completed","usage":{"input_tokens":7,"output_tokens":2}}\n'
        expired = subprocess.TimeoutExpired(['codex'], .5, output=output, stderr=b'')
        events = []
        with patch('kernel.backends.subprocess.run', side_effect=expired):
            with self.assertRaisesRegex(ModelUnavailable, 'timeout'):
                codex_completion('Return 5', timeout=.5, events=events)
        trace = inspect_trace('\n'.join(json.dumps(event) for event in events))
        self.assertEqual(trace['tokens'], 9)
        self.assertEqual(events[0]['type'], 'turn.completed')

    def test_explicit_proxy_environment_takes_precedence(self):
        from kernel.backends import codex_environment
        with patch.dict(os.environ, {'https_proxy':'http://explicit:8080', 'NO_PROXY':'internal'}, clear=True):
            with patch('urllib.request.getproxies', return_value={'https':'http://system:7897'}):
                environment = codex_environment()
            self.assertEqual(environment['https_proxy'], 'http://explicit:8080')
            self.assertNotIn('HTTPS_PROXY', environment)
            self.assertEqual(environment['NO_PROXY'], 'internal')

    def test_codex_adapter_keeps_model_and_counts_native_events(self):
        from kernel.backends import codex_completion
        fake = self.root / 'codex'
        fake.write_text('#!' + sys.executable + '\n' + """import json,sys
from pathlib import Path
assert '-m' in sys.argv and sys.argv[sys.argv.index('-m')+1]=='requested-model'
assert '--json' in sys.argv
out=Path(sys.argv[sys.argv.index('-o')+1])
out.write_text('5')
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':20,'cached_input_tokens':10,'output_tokens':4}}))
""")
        fake.chmod(0o755)
        with patch.dict(os.environ, {'PATH':str(self.root) + os.pathsep + os.environ['PATH']}):
            text, usage = codex_completion('Return 5', model='requested-model', timeout=10)
        self.assertEqual(text, '5')
        self.assertEqual(usage['input_tokens'], 20)
        self.assertEqual(usage['output_tokens'], 4)  # cached tokens are already included

    def test_round_reuses_trial_zero_without_duplicate_model_calls(self):
        self.reply = '\"wrong\"'
        engine = Engine(self.root / 'state',
                        [sys.executable, '-B', str(SOURCE / 'examples/demo_coder.py')],
                        task_command=self.command(), trials=1, timeout=30)
        with contextlib.redirect_stdout(io.StringIO()):
            record = engine.run(1)[0]
        self.assertFalse(record['accepted'])
        self.assertIn('candidate', record)
        self.assertEqual(len(self.requests), 24)

    def test_demo_rejects_custom_reviewer_before_invocation(self):
        marker = self.root / 'reviewer-ran'
        reviewer = shlex.join([sys.executable, '-c',
                               f'from pathlib import Path; Path({str(marker)!r}).touch()'])
        result = subprocess.run([sys.executable, '-m', 'kernel', '--state', str(self.root / 'state'),
                                 'run', '--demo', '--rounds', '1', '--reviewer', reviewer],
                                capture_output=True, text=True, cwd=SOURCE)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(marker.exists())

    def test_codex_missing_message_preserves_reported_tokens(self):
        fake = self.root / 'codex'
        fake.write_text('#!' + sys.executable + '\n' +
                        "import json; print(json.dumps({'type':'turn.completed','usage':{'input_tokens':7,'output_tokens':2}}))\n")
        fake.chmod(0o755)
        engine = Engine(self.root / 'state', task_command=[
            sys.executable, str(SOURCE / 'kernel/backends.py'), 'codex', '--role', 'task'])
        with patch.dict(os.environ, {'PATH':str(self.root) + os.pathsep + os.environ['PATH']}):
            with self.assertRaisesRegex(RuntimeError, 'model unavailable'):
                engine.solve({'operation':'sum','observation':'{\"values\":[2,3]}'})
        usage = json.loads((engine.root / 'usage.json').read_text())
        self.assertEqual(usage['tokens'], 9)
        self.assertEqual(usage['unknown_tokens'], 0)

    def test_task_budget_blocks_before_second_http_request(self):
        engine = Engine(self.root / 'state', task_command=self.command(), max_calls=1)
        engine.solve({'operation':'sum','observation':'{"values":[2,3]}'})
        with self.assertRaisesRegex(RuntimeError, 'budget'):
            engine.solve({'operation':'sum','observation':'{"values":[2,3]}'})
        self.assertEqual(len(self.requests), 1)


if __name__ == '__main__':
    unittest.main()
