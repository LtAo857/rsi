import contextlib
import csv
from http.server import BaseHTTPRequestHandler, HTTPServer
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import unittest

from kernel.ablation import confirmed_ablation_hints
from kernel.backends import ModelUnavailable, deepseek_completion
from kernel.diagnostics import diagnose_trace, verify_pagination_arithmetic
from kernel.engine import Engine, SOURCE
from kernel.isolation import limited_python, linux_sandbox_command
from kernel.ledger import check_post_call_limits, read_usage, reserve_call, settle_call
from kernel.pagination_tasks import make_extended_pagination_tasks
from kernel.reporting import compare_states, summarize_state
from kernel.search import SearchSettings
from kernel.semantic import validate_review


class ExtendedPaginationModel(Engine):
    """Offline in-process task model fixture for pagination_extended tests."""

    def __init__(self, *args, first_page_only=False, use_aggregate=True, **kwargs):
        self.first_page_only = first_page_only
        self.use_aggregate = use_aggregate
        super().__init__(*args, task_command=['offline-extended-pagination'], **kwargs)

    def model_response(self, root, messages, command=None):
        task = json.loads(messages['prompt'])
        history = task['history']
        pages = [row['result'] for row in history if isinstance(row['result'].get('rows'), list)]
        incomplete = bool(pages) and pages[-1]['next_offset'] is not None
        if not pages or (incomplete and not self.first_page_only):
            offset = pages[-1]['next_offset'] if pages else 0
            return json.dumps({'tool': 'read_csv', 'args': {'path': 'records.csv', 'offset': offset}})
        agg_results = [row['result'] for row in history if row['action'].get('tool') == 'aggregate' and 'value' in row['result']]
        if agg_results:
            return json.dumps({'answer': agg_results[-1]['value']})
        where = {'status': 'active'}
        region = re.search(r'region (north|south|east|west)\b', task['question'])
        if region:
            where['region'] = region.group(1)
        priority = re.search(r'priority (high|medium|low)\b', task['question'])
        if priority:
            where['priority'] = priority.group(1)
        op = 'sum' if task['question'].startswith('Sum') else 'count'
        if self.use_aggregate and not self.first_page_only:
            args = {'path': 'records.csv', 'operation': op, 'where': where}
            if op == 'sum':
                args['field'] = 'qty'
            return json.dumps({'tool': 'aggregate', 'args': args})
        rows = [row for page in pages for row in page['rows'] if all(row[k] == v for k, v in where.items())]
        answer = sum(int(row['qty']) for row in rows) if op == 'sum' else len(rows)
        return json.dumps({'answer': answer})


class EnhancementTests(unittest.TestCase):
    def test_pagination_extended_is_reproducible_and_requires_later_pages(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            first = make_extended_pagination_tasks(Path(a), 456)
            second = make_extended_pagination_tasks(Path(b), 456)
            self.assertEqual(len(first), 6)
            self.assertEqual([item['id'] for item in first], [
                'px-active_sum', 'px-active_count',
                'px-region_active_sum', 'px-region_active_count',
                'px-priority_active_sum', 'px-priority_active_count',
            ])
            for x, y in zip(first, second):
                self.assertEqual(x['digest'], y['digest'])
                self.assertEqual((x['directory'] / 'records.csv').read_bytes(),
                                 (y['directory'] / 'records.csv').read_bytes())
                with (x['directory'] / 'records.csv').open() as stream:
                    rows = list(csv.DictReader(stream))
                self.assertGreaterEqual(len(rows), 65)
                self.assertLessEqual(len(rows), 95)
                self.assertEqual(rows[-1]['status'], 'active')

    def test_pagination_extended_engine_scoring_heldout_and_lock(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            full_engine = ExtendedPaginationModel(a, benchmark='datadesk', datadesk_size='pagination_extended')
            full_engine.initialize()
            root = full_engine.version(full_engine.state()['active'])
            search_res = full_engine.score(root, 101)
            heldout_res = full_engine.score(root, 101, heldout=True)
            self.assertEqual(search_res['score'], 1.0)
            self.assertEqual(heldout_res['score'], 1.0)
            self.assertEqual(search_res['size'], 'pagination_extended')
            self.assertEqual(len(search_res['traces']), 6)
            self.assertNotEqual(search_res['data_seed'], heldout_res['data_seed'])
            self.assertEqual(Engine(a).datadesk_size, 'pagination_extended')
            with self.assertRaises(ValueError):
                Engine(a, datadesk_size='pagination')

            early_engine = ExtendedPaginationModel(b, first_page_only=True, benchmark='datadesk', datadesk_size='pagination_extended')
            early_engine.initialize()
            early_res = early_engine.score(early_engine.version(early_engine.state()['active']), 101)
            self.assertEqual(early_res['score'], 0.0)
            for trace in early_res['traces']:
                self.assertIn('unread_pages', {f['kind'] for f in trace['diagnostics']['findings']})

    def test_enhanced_deterministic_diagnostics(self):
        # 1. sql_without_schema fires only when sql_error precedes schema.
        pre_schema = diagnose_trace({
            'ok': True, 'stop_reason': 'answer', 'history': [
                {'action': {'tool': 'sql', 'args': {'query': 'SELECT bad FROM orders'}}, 'result': {'error': 'no such column'}},
                {'action': {'tool': 'schema', 'args': {}}, 'result': {'tables': {'orders': ['qty']}}},
                {'action': {'tool': 'sql', 'args': {'query': 'SELECT COUNT(*) FROM orders'}}, 'result': {'rows': [[1]]}},
            ]
        })
        kinds = [f['kind'] for f in pre_schema['findings']]
        self.assertEqual(kinds[0], 'sql_error')
        self.assertIn('sql_without_schema', kinds)
        post_schema = diagnose_trace({
            'ok': True, 'stop_reason': 'answer', 'history': [
                {'action': {'tool': 'schema', 'args': {}}, 'result': {'tables': {'orders': ['qty']}}},
                {'action': {'tool': 'sql', 'args': {'query': 'SELECT bad FROM orders'}}, 'result': {'error': 'no such column'}},
            ]
        })
        self.assertNotIn('sql_without_schema', {f['kind'] for f in post_schema['findings']})

        # 2. untyped_sql_numeric_risk fires on raw ORDER BY qty/unit_price, not on CAST.
        untyped = diagnose_trace({
            'ok': False, 'stop_reason': 'answer', 'history': [
                {'action': {'tool': 'sql', 'args': {'query': 'SELECT order_id FROM orders ORDER BY qty DESC LIMIT 1'}},
                 'result': {'rows': [['O1']]}},
            ]
        })
        self.assertIn('untyped_sql_numeric_risk', {f['kind'] for f in untyped['findings']})
        casted = diagnose_trace({
            'ok': True, 'stop_reason': 'answer', 'history': [
                {'action': {'tool': 'sql', 'args': {'query': 'SELECT order_id FROM orders ORDER BY CAST(qty AS INTEGER) DESC LIMIT 1'}},
                 'result': {'rows': [['O1']]}},
            ]
        })
        self.assertNotIn('untyped_sql_numeric_risk', {f['kind'] for f in casted['findings']})

        # 3. manual_pagination_aggregation and extended arithmetic verification.
        rows = [
            {'id': '0', 'status': 'active', 'region': 'north', 'priority': 'high', 'qty': '10'},
            {'id': '1', 'status': 'active', 'region': 'south', 'priority': 'low', 'qty': '5'},
            {'id': '2', 'status': 'active', 'region': 'north', 'priority': 'low', 'qty': '7'},
        ]
        trace = {
            'task': {'id': 'px-region_active_sum', 'question': 'Sum qty across all records with status active and region north.'},
            'ok': False, 'stop_reason': 'answer', 'answer': '999',
            'history': [
                {'action': {'tool': 'read_csv', 'args': {'path': 'records.csv', 'offset': 0}},
                 'result': {'rows': rows, 'next_offset': None}},
            ],
        }
        diag = diagnose_trace(trace)
        diag_kinds = {f['kind'] for f in diag['findings']}
        self.assertIn('manual_pagination_aggregation', diag_kinds)
        self.assertIn('verified_arithmetic_mismatch', diag_kinds)
        self.assertIsNotNone(verify_pagination_arithmetic(trace))

        # When aggregate was called, manual_pagination_aggregation must not fire.
        trace_with_agg = {
            **trace,
            'history': trace['history'] + [
                {'action': {'tool': 'aggregate', 'args': {'path': 'records.csv', 'operation': 'sum', 'field': 'qty',
                                                          'where': {'status': 'active', 'region': 'north'}}},
                 'result': {'value': '17', 'complete': True}},
            ],
        }
        self.assertNotIn('manual_pagination_aggregation', {f['kind'] for f in diagnose_trace(trace_with_agg)['findings']})

    def test_confirmed_ablation_hints_feed_pruning_round_only_for_active_version(self):
        spec = {'name': 'trim_hint', 'path': 'agent/solver.py', 'before': 'old', 'after': 'o'}
        report = {
            'active': 'v1', 'status': 'complete', 'specs': [spec],
            'variants': [
                {'name': 'trim_hint', 'confirmed_task_benefit': True, 'observed_regressions': 0, 'deletion_authorized': False},
            ],
        }
        hints = confirmed_ablation_hints(report, 'v1')
        self.assertEqual(len(hints), 1)
        self.assertFalse(hints[0]['deletion_authorized'])
        self.assertEqual(confirmed_ablation_hints(report, 'v2'), [])
        self.assertEqual(confirmed_ablation_hints({**report, 'status': 'failed'}, 'v1'), [])
        regressed = {**report, 'variants': [{**report['variants'][0], 'observed_regressions': 1}]}
        self.assertEqual(confirmed_ablation_hints(regressed, 'v1'), [])

        class RecordingEngine(Engine):
            def code(self, workspace, prompt, log, command=None):
                if (workspace / 'agent').exists():
                    self.evidence = json.loads(prompt.rsplit('Evidence:\n', 1)[1])
                return super().code(workspace, prompt, log, command)

        with tempfile.TemporaryDirectory() as directory:
            engine = RecordingEngine(directory, [sys.executable, str(SOURCE / 'examples/demo_coder.py')],
                                     trials=1, prune_every=1)
            engine.initialize()
            active = engine.state()['active']
            (engine.root / 'ablation.json').write_text(json.dumps({**report, 'active': active}))
            with contextlib.redirect_stdout(io.StringIO()):
                engine.run(1)
            self.assertIn('ablation_hints', engine.evidence)
            self.assertEqual(engine.evidence['ablation_hints'][0]['name'], 'trim_hint')
            self.assertFalse(engine.evidence['ablation_hints'][0]['deletion_authorized'])

    def test_report_and_compare_cli_are_read_only_and_accurate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_a = root / 'state-a'
            state_b = root / 'state-b'
            demo = [sys.executable, str(SOURCE / 'examples/demo_coder.py')]
            eng_a = Engine(state_a, demo, trials=1)
            eng_b = Engine(state_b, demo, trials=1)
            with contextlib.redirect_stdout(io.StringIO()):
                eng_a.run(1)
                eng_b.run(2)
            before_files = sorted(p.relative_to(state_a).as_posix() for p in state_a.rglob('*'))
            rep_proc = subprocess.run(
                [sys.executable, '-m', 'kernel', '--state', str(state_a), 'report'],
                cwd=SOURCE, capture_output=True, text=True,
            )
            self.assertEqual(rep_proc.returncode, 0, rep_proc.stderr)
            summary = json.loads(rep_proc.stdout)
            self.assertEqual(summary['rounds_completed'], 1)
            self.assertEqual(summary['accepted_rounds'], 1)
            self.assertEqual(sorted(p.relative_to(state_a).as_posix() for p in state_a.rglob('*')), before_files)

            cmp_proc = subprocess.run(
                [sys.executable, '-m', 'kernel', 'compare', str(state_a), str(state_b)],
                cwd=SOURCE, capture_output=True, text=True,
            )
            self.assertEqual(cmp_proc.returncode, 0, cmp_proc.stderr)
            compared = json.loads(cmp_proc.stdout)
            self.assertEqual(compared['comparison']['rounds_delta'], 1)
            self.assertFalse(compared['comparison']['same_active_version'])
            self.assertTrue(compared['comparison']['same_runtime'])
            self.assertEqual(summarize_state(state_a)['active'], compared['left']['active'])
            self.assertEqual(compare_states(state_a, state_b)['comparison']['rounds_delta'], 1)

    def test_ledger_reserve_settle_and_post_call_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            usage_path = Path(directory) / 'usage.json'
            settings = SearchSettings(max_calls=2, max_tokens=100, max_call_tokens=60)
            usage = reserve_call(usage_path, settings)
            self.assertEqual(usage['calls'], 1)
            self.assertEqual(usage['unknown_tokens'], 1)
            settle_call(usage_path, usage, {'tokens': 40, 'cost_usd': None})
            check_post_call_limits(usage, {'tokens': 40, 'cost_usd': None}, settings)
            self.assertEqual(read_usage(usage_path)['tokens'], 40)
            self.assertEqual(read_usage(usage_path)['unknown_tokens'], 0)
            usage2 = reserve_call(usage_path, settings)
            settle_call(usage_path, usage2, {'tokens': 70, 'cost_usd': None})
            with self.assertRaisesRegex(RuntimeError, 'budget exceeded: tokens'):
                check_post_call_limits(usage2, {'tokens': 70, 'cost_usd': None}, settings)
            with self.assertRaisesRegex(RuntimeError, 'budget exhausted: calls'):
                reserve_call(usage_path, settings)

    def test_linux_bwrap_command_builder_and_fail_closed_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake_bwrap = root / 'bwrap'
            fake_bwrap.write_text('#!/bin/sh\n')
            ro_dir = root / 'ro'
            rw_dir = root / 'rw'
            ro_dir.mkdir()
            rw_dir.mkdir()
            cmd = linux_sandbox_command(
                ['/usr/bin/python3', '-I', '-c', 'print(1)'],
                readable=[ro_dir], writable=[rw_dir],
                bwrap_path=fake_bwrap, platform='linux',
            )
            self.assertEqual(cmd[0], str(fake_bwrap))
            self.assertIn('--unshare-all', cmd)
            self.assertIn('--clearenv', cmd)
            self.assertIn(str(ro_dir.resolve()), cmd)
            self.assertIn(str(rw_dir.resolve()), cmd)
            # Non-linux platform or missing binary fails closed.
            with self.assertRaisesRegex(ValueError, 'Linux bwrap sandbox unavailable'):
                linux_sandbox_command(['/bin/true'], bwrap_path=fake_bwrap, platform='darwin')
            with self.assertRaisesRegex(ValueError, 'Linux bwrap sandbox unavailable'):
                linux_sandbox_command(['/bin/true'], bwrap_path=root / 'missing', platform='linux')
            # Memory cap code is emitted cleanly when requested.
            prog = limited_python('print(1)', memory_bytes=536870912)
            self.assertIn('RLIMIT_AS', prog)
            if sys.platform != 'linux':
                with self.assertRaisesRegex(ValueError, 'Linux bwrap sandbox unavailable'):
                    Engine(root / 'state', host_sandbox='linux-bwrap')

    def test_versions_list_and_manual_rollback_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / 'state'
            demo = [sys.executable, str(SOURCE / 'examples/demo_coder.py')]
            engine = Engine(state_dir, demo, trials=1)
            engine.initialize()
            initial = engine.state()['active']
            with contextlib.redirect_stdout(io.StringIO()):
                engine.run(1)
            upgraded = engine.state()['active']
            self.assertNotEqual(initial, upgraded)
            vers_proc = subprocess.run(
                [sys.executable, '-m', 'kernel', '--state', str(state_dir), 'versions'],
                cwd=SOURCE, capture_output=True, text=True,
            )
            self.assertEqual(vers_proc.returncode, 0, vers_proc.stderr)
            vers_data = json.loads(vers_proc.stdout)
            self.assertEqual(len(vers_data['versions']), 2)
            self.assertTrue(all(v['valid'] for v in vers_data['versions']))

            rb_proc = subprocess.run(
                [sys.executable, '-m', 'kernel', '--state', str(state_dir), 'rollback', initial],
                cwd=SOURCE, capture_output=True, text=True,
            )
            self.assertEqual(rb_proc.returncode, 0, rb_proc.stderr)
            rb_data = json.loads(rb_proc.stdout)
            self.assertEqual(rb_data['active'], initial)
            self.assertEqual(rb_data['previous'], upgraded)
            self.assertTrue(rb_data['changed'])
            self.assertEqual(engine.state()['active'], initial)

    def test_multi_candidate_round_reuses_baseline_and_activates_second_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            counter = root / 'count.txt'
            script = root / 'flaky_coder.py'
            script.write_text(
                "import json, pathlib, subprocess, sys\n"
                f"counter = pathlib.Path({str(counter)!r})\n"
                "prompt = sys.stdin.read()\n"
                "if pathlib.Path('solution.py').exists():\n"
                f"    sys.exit(subprocess.run([sys.executable, {str(SOURCE / 'examples/demo_coder.py')!r}], input=prompt, text=True).returncode)\n"
                "n = int(counter.read_text()) if counter.exists() else 0\n"
                "counter.write_text(str(n + 1))\n"
                "if n == 0:\n"
                "    # First candidate omits proposal so it fails proposal_validation.\n"
                "    sys.exit(0)\n"
                f"sys.exit(subprocess.run([sys.executable, {str(SOURCE / 'examples/demo_coder.py')!r}], input=prompt, text=True).returncode)\n"
            )
            engine = Engine(root / 'state', [sys.executable, str(script)], trials=1, candidates=2)
            with contextlib.redirect_stdout(io.StringIO()):
                record = engine.run(1)[0]
            self.assertTrue(record['accepted'])
            self.assertEqual(len(record['attempts']), 2)
            self.assertFalse(record['attempts'][0]['accepted'])
            self.assertTrue(record['attempts'][1]['accepted'])

    def test_deepseek_retries_transient_http_and_semantic_missing_data_check(self):
        attempts = [0]
        class FlakyHandler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                self.rfile.read(int(self.headers.get('Content-Length', 0)))
                attempts[0] += 1
                if attempts[0] == 1:
                    self.send_response(503)
                    self.end_headers()
                    return
                body = json.dumps({
                    'choices': [{'finish_reason': 'stop', 'message': {'content': 'ok'}}],
                    'usage': {'prompt_tokens': 3, 'completion_tokens': 2},
                }).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        server = HTTPServer(('127.0.0.1', 0), FlakyHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f'http://127.0.0.1:{server.server_port}'
            os.environ['RSI_RETRY_TEST_KEY'] = 'test-secret'
            text, usage = deepseek_completion('hi', 'test-model', url, 'RSI_RETRY_TEST_KEY', retries=1, retry_delay=0.01)
            self.assertEqual(text, 'ok')
            self.assertEqual(attempts[0], 2)
            self.assertEqual(usage['input_tokens'], 3)
            # Qwen models also receive chat_template_kwargs.enable_thinking when thinking is set.
            captured = []
            orig_post = FlakyHandler.do_POST
            def capture_post(handler):
                length = int(handler.headers.get('Content-Length', 0))
                captured.append(json.loads(handler.rfile.read(length)))
                body = json.dumps({
                    'choices': [{'finish_reason': 'stop', 'message': {'content': 'ok'}}],
                    'usage': {'prompt_tokens': 2, 'completion_tokens': 1},
                }).encode()
                handler.send_response(200)
                handler.send_header('Content-Type', 'application/json')
                handler.send_header('Content-Length', str(len(body)))
                handler.end_headers()
                handler.wfile.write(body)
            FlakyHandler.do_POST = capture_post
            try:
                deepseek_completion('hi', 'qwen3.6-35b-a3b', url, 'RSI_RETRY_TEST_KEY', thinking='disabled')
                self.assertEqual(captured[-1]['chat_template_kwargs'], {'enable_thinking': False})
            finally:
                FlakyHandler.do_POST = orig_post
        finally:
            server.shutdown()
            server.server_close()
            os.environ.pop('RSI_RETRY_TEST_KEY', None)

        trace = {'history': [{'action': {'tool': 'aggregate', 'args': {}}, 'result': {'value': '1'}}]}
        bad = json.dumps({'findings': [{'kind': 'missing_data', 'steps': [0], 'hypothesis': 'missed pages'}]})
        with self.assertRaisesRegex(ValueError, 'missing_data hypothesis'):
            validate_review(bad, trace)
        good_trace = {'history': [{'action': {'tool': 'read_csv', 'args': {'path': 'a.csv'}}, 'result': {'rows': []}}]}
        good = json.dumps({'findings': [{'kind': 'missing_data', 'steps': [0, 0], 'hypothesis': 'missed pages'}]})
        validated = validate_review(good, good_trace)
        self.assertEqual(validated['findings'][0]['steps'], [0])
        self.assertEqual(validated['findings'][0]['status'], 'unverified_hypothesis')

    def test_hard_datadesk_profile_generates_and_scores_new_sql_families(self):
        from tests.fixtures.datadesk_model import query_for

        def hard_query(q):
            m = re.search(r"customers living in ([A-Za-z]+)\?", q)
            if m:
                return ("SELECT ROUND(SUM(CAST(o.qty AS REAL) * CAST(o.unit_price AS REAL)), 2) "
                        "FROM orders o JOIN customers c ON o.customer_id = c.customer_id "
                        f"WHERE o.status = 'completed' AND c.city = '{m.group(1)}'")
            if "highest total profit" in q:
                return ("SELECT o.product FROM orders o JOIN products p ON o.product = p.product "
                        "WHERE o.status = 'completed' GROUP BY o.product "
                        "ORDER BY SUM(CAST(o.qty AS REAL) * (CAST(o.unit_price AS REAL) - CAST(p.unit_cost AS REAL))) DESC LIMIT 1")
            if "at least 2 completed orders" in q:
                return ("SELECT COUNT(*) FROM (SELECT customer_id FROM orders "
                        "WHERE status = 'completed' GROUP BY customer_id HAVING COUNT(*) >= 2)")
            return query_for(q)

        class HardSqlEngine(Engine):
            def model_response(self, root, messages, command=None):
                task = json.loads(messages['prompt'])
                history = task['history']
                if not history:
                    return json.dumps({'tool': 'schema', 'args': {}})
                if 'rows' in history[-1]['result']:
                    return json.dumps({'answer': history[-1]['result']['rows'][0][0]})
                return json.dumps({'tool': 'sql', 'args': {'query': hard_query(task['question'])}})

        with tempfile.TemporaryDirectory() as directory:
            engine = HardSqlEngine(directory, task_command=['offline-hard-sql'],
                                   benchmark='datadesk', datadesk_size='hard')
            engine.initialize()
            root = engine.version(engine.state()['active'])
            search_res = engine.score(root, 101)
            heldout_res = engine.score(root, 202, heldout=True)
            self.assertEqual(len(search_res['traces']), 8)
            self.assertEqual(len(heldout_res['traces']), 10)
            self.assertEqual(search_res['score'], 1.0)
            self.assertEqual(heldout_res['score'], 1.0)
            self.assertEqual(Engine(directory).datadesk_size, 'hard')
            with self.assertRaises(ValueError):
                Engine(directory, datadesk_size='compact')

    def test_diff_cli_and_multi_candidate_failure_feedback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_dir = root / 'state'
            prompts_log = root / 'prompts.json'
            script = root / 'feedback_coder.py'
            script.write_text(
                "import json, pathlib, subprocess, sys\n"
                f"log_path = pathlib.Path({str(prompts_log)!r})\n"
                "prompt = sys.stdin.read()\n"
                "if pathlib.Path('solution.py').exists():\n"
                f"    sys.exit(subprocess.run([sys.executable, {str(SOURCE / 'examples/demo_coder.py')!r}], input=prompt, text=True).returncode)\n"
                "seen = json.loads(log_path.read_text()) if log_path.exists() else []\n"
                "seen.append(prompt)\n"
                "log_path.write_text(json.dumps(seen))\n"
                "if len(seen) == 1:\n"
                "    sys.exit(0)\n"
                f"sys.exit(subprocess.run([sys.executable, {str(SOURCE / 'examples/demo_coder.py')!r}], input=prompt, text=True).returncode)\n"
            )
            engine = Engine(state_dir, [sys.executable, str(script)], trials=1, candidates=2)
            with contextlib.redirect_stdout(io.StringIO()):
                record = engine.run(1)[0]
            self.assertTrue(record['accepted'])
            seen = json.loads(prompts_log.read_text())
            self.assertEqual(len(seen), 2)
            self.assertNotIn('Previous candidate attempts in this round failed', seen[0])
            self.assertIn('Previous candidate attempts in this round failed', seen[1])
            self.assertIn('proposal_validation', seen[1])

            diff_proc = subprocess.run(
                [sys.executable, '-m', 'kernel', '--state', str(state_dir), 'diff'],
                cwd=SOURCE, capture_output=True, text=True,
            )
            self.assertEqual(diff_proc.returncode, 0, diff_proc.stderr)
            diff_data = json.loads(diff_proc.stdout)
            self.assertNotEqual(diff_data['left'], diff_data['right'])
            self.assertIn('agent/solver.py', diff_data['stats']['files'])
            self.assertIn('--- before/agent/solver.py', diff_data['patch'])


if __name__ == '__main__':
    unittest.main()
