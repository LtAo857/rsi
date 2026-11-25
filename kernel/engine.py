"""Trusted ratchet: snapshots, evaluation, meta-probes, activation and recovery.

Inspired by agent-rsi's frozen evaluation, fresh confirmation and candidate
isolation. Mutable generations contain both the solver and proposal policy.
Only this supervisor can publish a new generation.
"""

from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

from .backends import ModelUnavailable
from .execution import CallLimit, run_bounded
from .search import (TraceFailure, SearchSettings, confirmed_gain, inspect_trace, patch_stats,
                     review_proposal, candidate_workspace)
from .evaluation import answers_equal, heldout_repair_cases, repair_case, tasks
from .datadesk import PaginatedToolbox, Toolbox, check_answer, run_session
from .pagination_tasks import make_extended_pagination_tasks, make_pagination_tasks
from .datadesk_tasks import EVOLVE, HELDOUT_ONLY, make_split
from .diagnostics import compact_evidence, diagnose_evaluation, task_failure_evidence
from .ablation import apply_spec, assess_pairs, confirmed_ablation_hints, usage_delta, validate_specs
from .isolation import clean_environment, limited_python, sandbox_command, trusted_adapter
from .ledger import check_post_call_limits, read_usage, reserve_call, settle_call
from .semantic import review_messages, validate_review
from .store import (REQUIRED, archive_failure as store_archive_failure, atomic_json, diff_trees,
                    file_lock, identity, manifest, validate_tree as store_validate_tree)
from .reporting import write_markdown_report

SOURCE = Path(__file__).resolve().parents[1]
WORKER = Path(__file__).with_name("worker.py")


class Engine:
    def __init__(self, state_dir, command=None, timeout=300, task_command=None, benchmark=None, datadesk_size=None, host_sandbox=None, **settings):
        self.root = Path(state_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.versions = self.root / "versions"
        self.versions.mkdir(exist_ok=True)
        self.state_path = self.root / "state.json"
        self.command = command
        self.timeout = timeout
        self.search = SearchSettings(**settings)
        runtime_path = self.root / 'runtime.json'
        saved = json.loads(runtime_path.read_text()) if runtime_path.exists() else None
        saved_sandbox = saved.get('host_sandbox','none') if saved else 'none'
        self.host_sandbox = host_sandbox or saved_sandbox
        if self.host_sandbox not in ('none', 'macos', 'linux-bwrap'):
            raise ValueError('unknown host sandbox')
        if (saved or self.state_path.exists()) and self.host_sandbox != saved_sandbox:
            raise ValueError('sandbox configuration changed; use a new state directory')
        if self.host_sandbox in ('macos', 'linux-bwrap'):
            probe = subprocess.run(sandbox_command([sys.executable,'-I','-B','-c','print("ready")'], backend=self.host_sandbox),
                                   capture_output=True,text=True,timeout=10,env=clean_environment())
            if probe.returncode or probe.stdout.strip() != 'ready':
                raise ValueError('sandbox probe failed; refusing unsandboxed execution')
            for adapter in (command,task_command,settings.get('reviewer'),settings.get('semantic_reviewer')):
                if adapter:
                    trusted_adapter(adapter,SOURCE)
        saved_benchmark = saved.get('benchmark', 'basic') if saved else 'basic'
        self.benchmark = benchmark or saved_benchmark
        if self.benchmark not in ('basic', 'advanced', 'datadesk'):
            raise ValueError('unknown benchmark suite')
        saved_size = saved.get('datadesk_size', 'compact') if saved else 'compact'
        self.datadesk_size = datadesk_size or saved_size
        if self.datadesk_size not in ('compact', 'full', 'pagination', 'pagination_extended'):
            raise ValueError('unknown DataDesk size')
        if (saved or self.state_path.exists()) and self.datadesk_size != saved_size:
            raise ValueError('DataDesk size changed; use a new state directory')
        if (saved or self.state_path.exists()) and self.benchmark != saved_benchmark:
            raise ValueError('benchmark changed; use a new state directory')
        if saved is not None and task_command is not None and saved['task_command'] != task_command:
            raise ValueError('task model configuration changed; use a new state directory')
        if saved is None and task_command is not None and self.state_path.exists():
            raise ValueError('cannot add a task model to an existing experiment; use a new state directory')
        self.task_command = saved['task_command'] if saved is not None else task_command
        if self.benchmark == 'datadesk' and not self.task_command:
            raise ValueError('DataDesk requires a task model backend; choose --task-backend')
        # Persist at initialization under the writer lock, not in a read-only constructor.


    @contextmanager
    def lock(self):
        with file_lock(self.root / "LOCK"):
            yield

    def event(self, **record):
        record["time"] = time.time()
        with (self.root / "history.jsonl").open("a") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")

    def state(self):
        return json.loads(self.state_path.read_text())

    def version(self, version_id):
        if len(version_id) != 64 or any(c not in "0123456789abcdef" for c in version_id):
            raise ValueError("invalid generation id")
        return self.versions / version_id

    def call(self, root, op, **arguments):
        use_model = arguments.pop('use_model', True)
        if op == 'solve' and arguments['task'].get('kind') == 'datadesk':
            task = arguments['task']
            return self.datadesk_session(root, task['question'], task['data_dir'])
        if op == 'solve' and self.task_command and use_model:
            messages = self.call(root, 'messages', task=arguments['task'])
            arguments['model_response'] = self.model_response(root, messages)
        before = identity(root)
        request = json.dumps({"root": str(root), "op": op, **arguments})
        if self.host_sandbox in ('macos', 'linux-bwrap'):
            with tempfile.TemporaryDirectory(prefix='rsi-worker-') as scratch:
                memory_cap = 536870912 if self.host_sandbox == 'linux-bwrap' else None
                program = limited_python('import runpy; runpy.run_path('+repr(str(WORKER))+',run_name="__main__")',
                                         memory_bytes=memory_cap)
                command = sandbox_command([sys.executable,'-I','-B','-c',program],
                                          readable=[root,WORKER],writable=[scratch],backend=self.host_sandbox)
                process = run_bounded(command,request,root,min(self.timeout,30),1048576,env=clean_environment())
        else:
            process = subprocess.run([sys.executable,'-I','-B',str(WORKER)],input=request,
                                     text=True,capture_output=True,cwd=root,timeout=min(self.timeout,30))
        if identity(root) != before:
            raise RuntimeError("generation modified its source during execution")
        if process.returncode:
            raise RuntimeError(process.stderr[-2000:])
        return json.loads(process.stdout)["result"]

    def model_response(self, root, messages, command=None):
        if not (self.task_command or command):
            raise ModelUnavailable('DataDesk requires a task model backend')
        if (not isinstance(messages, dict) or set(messages) != {'system', 'prompt'} or
                any(not isinstance(value, str) for value in messages.values())):
            raise ValueError('build_messages(task) must return system and prompt strings')
        if len(json.dumps(messages)) > 100_000:
            raise ValueError('task model prompt exceeds 100KB')
        logs = self.root / 'model-calls'
        logs.mkdir(exist_ok=True)
        fingerprint = identity(root)
        try:
            output = self.code(root, json.dumps(messages, ensure_ascii=False),
                               logs / (uuid.uuid4().hex + '.log'), command=command or self.task_command)
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
            raise ModelUnavailable('model unavailable: ' + str(error)) from error
        if identity(root) != fingerprint:
            raise RuntimeError('task model adapter modified generation source')
        responses = []
        for line in output.splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and isinstance(row.get('model_response'), str):
                responses.append(row['model_response'])
        if len(responses) != 1:
            raise ModelUnavailable('model unavailable: adapter returned no unique response')
        return responses[0]

    def datadesk_session(self, root, question, data_dir):
        if not isinstance(question, str) or not question.strip() or len(question) > 10000:
            raise ValueError('invalid DataDesk question')
        tools = (PaginatedToolbox if self.datadesk_size in ('pagination', 'pagination_extended') else Toolbox)(data_dir)
        def choose(context):
            messages = self.call(root, 'messages', task=context)
            text = self.model_response(root, messages)
            return self.call(root, 'action', text=text)
        try:
            return run_session(question, tools, choose)
        finally:
            tools.close()

    def health(self, root):
        for relative in REQUIRED:
            if not (root / relative).is_file():
                raise ValueError("missing " + relative)
        self.call(root, "plan", evidence={}, goal="health check")
        self.analyze(root, {'failures': [{'id': 'health', 'kind': 'maintenance'}]})
        if self.benchmark == 'datadesk':
            messages = self.call(root, 'messages', task={'kind':'datadesk', 'question':'health', 'history':[]})
            if not isinstance(messages, dict) or set(messages) != {'system', 'prompt'} or any(not isinstance(v, str) for v in messages.values()):
                raise ValueError('invalid DataDesk messages')
            if self.call(root, 'action', text='{"answer":"health"}') != {'answer':'health'}:
                raise ValueError('DataDesk parse_action health check failed')
        value = self.call(root, "solve", use_model=False, task={"operation": "sum",
                                              "observation": '{"values": [2, 3]}'})
        if self.task_command:
            self.call(root, 'messages', task={'operation':'sum', 'observation':'{"values":[2,3]}'})
        if value != 5:
            raise ValueError("basic solver health check failed")

    def analyze(self, root, evidence):
        # Never give a replacement policy the incumbent's answer to echo.
        evidence = {key: value for key, value in evidence.items() if key != 'strategy'}
        strategy = self.call(root, 'analyze', evidence=evidence)
        if strategy is None:  # Legacy generations may omit analyze entirely.
            return None
        known = {row['id'] for row in evidence.get('failures', [])}
        if (not isinstance(strategy, dict) or
                not isinstance(strategy.get('failure_ids'), list) or not strategy['failure_ids'] or
                any(not isinstance(value, str) or value not in known for value in strategy['failure_ids']) or
                strategy.get('target') not in ('agent', 'evolution') or
                not isinstance(strategy.get('hypothesis'), str) or not strategy['hypothesis'].strip()):
            raise ValueError('invalid mutable analysis strategy')
        return strategy

    def publish(self, candidate):
        self.validate_tree(candidate)
        version_id = identity(candidate)
        destination = self.version(version_id)
        if not destination.exists():
            staging = self.versions / ("staging-" + uuid.uuid4().hex)
            shutil.copytree(candidate, staging, ignore=shutil.ignore_patterns("__pycache__"))
            os.replace(staging, destination)
        return version_id

    def initialize(self):
        existing_runtime = self.root / 'runtime.json'
        if existing_runtime.exists():
            saved = json.loads(existing_runtime.read_text())
            if (saved.get('task_command') != self.task_command or
                    saved.get('benchmark','basic') != self.benchmark or
                    saved.get('datadesk_size','compact') != self.datadesk_size or
                    saved.get('host_sandbox','none') != self.host_sandbox):
                raise ValueError('runtime configuration changed before initialization; use a new state directory')
        if self.task_command or self.benchmark != 'basic' or self.host_sandbox != 'none':
            runtime = self.root / 'runtime.json'
            if runtime.exists() and json.loads(runtime.read_text())['task_command'] != self.task_command:
                raise ValueError('task model configuration changed; use a new state directory')
            if not runtime.exists():
                atomic_json(runtime, {'task_command':self.task_command, 'benchmark':self.benchmark,
                                      'datadesk_size':self.datadesk_size,'host_sandbox':self.host_sandbox})
        if self.state_path.exists():
            return self.state()
        with tempfile.TemporaryDirectory(prefix="rsi-seed-") as directory:
            seed = Path(directory)
            for name in ("agent", "evolution"):
                shutil.copytree(SOURCE / name, seed / name,
                                ignore=shutil.ignore_patterns("__pycache__"))
            self.health(seed)
            version_id = self.publish(seed)
        state = {"active": version_id, "previous": None, "round": 0}
        atomic_json(self.state_path, state)
        self.event(type="initialized", active=version_id)
        return state

    def recover(self, state):
        try:
            root = self.version(state["active"])
            if identity(root) != state["active"]:
                raise ValueError("active generation hash mismatch")
            self.health(root)
        except Exception as error:
            previous = state.get("previous")
            if not previous:
                raise RuntimeError("active version failed; no fallback available") from error
            root = self.version(previous)
            if identity(root) != previous:
                raise RuntimeError("fallback generation hash mismatch") from error
            self.health(root)
            failed = state["active"]
            state.update(active=previous, previous=None)
            atomic_json(self.state_path, state)
            self.event(type="rollback", failed=failed, active=previous, reason=str(error))
        return state

    def validate_tree(self, candidate):
        store_validate_tree(candidate, REQUIRED)

    def archive_failure(self, candidate, current, audit):
        return store_archive_failure(candidate, current, audit)

    def code(self, workspace, prompt, log, command=None):
        command = command or self.command
        if not command:
            raise RuntimeError("a coding command is required; use --coder or --demo")
        if self.host_sandbox in ('macos', 'linux-bwrap'):
            trusted_adapter(command,SOURCE)
            # Network-enabled adapters must not import candidate/user PYTHONPATH.
            command = [command[0],'-I',*command[1:]]
        if self.search.max_prompt_chars is not None and len(prompt) > self.search.max_prompt_chars:
            raise RuntimeError('call prompt character limit exceeded before launch')
        usage_path = self.root / "usage.json"
        usage = reserve_call(usage_path, self.search)
        interrupted = None
        try:
            process = run_bounded(command, prompt, workspace, self.timeout,
                                  self.search.max_output_bytes, self.search.max_call_tokens)
        except subprocess.TimeoutExpired as error:
            def decoded(value):
                return value.decode(errors="replace") if isinstance(value, bytes) else (value or "")
            log.write_text(decoded(error.stdout) + "\n" + decoded(error.stderr))
            atomic_json(log.with_suffix(".trace.json"), {"failures": ["timeout"],
                                                       "tokens": None, "cost_usd": None})
            raise
        except CallLimit as error:
            interrupted = error
            process = subprocess.CompletedProcess(command, -1, error.stdout, error.stderr)
        log.write_text(process.stdout + "\n" + process.stderr)
        trace = inspect_trace(process.stdout)
        if process.returncode:
            trace["failures"].append("command_error")
        atomic_json(log.with_suffix(".trace.json"), trace)
        settle_call(usage_path, usage, trace)
        if interrupted:
            raise interrupted
        if process.returncode:
            raise RuntimeError(f"coding command failed ({process.returncode}); see {log}")
        check_post_call_limits(usage, trace, self.search)
        if set(trace["failures"]) & {"give_up", "loop", "repeated_tool_call", "tool_error"}:
            raise TraceFailure("trace failure: " + ", ".join(trace["failures"]))
        return process.stdout

    def search_evidence(self):
        history = self.recent_history(limit=None)
        counts = {"agent": 0, "evolution": 0}
        streak = 0
        for row in history:
            audit = Path(row["audit"]) if row.get("audit") else None
            if audit:
                patch = audit / "changes.patch"
                if patch.exists():
                    row["patch"] = patch.read_text()
                traces = [json.loads(path.read_text()) for path in sorted(audit.glob("*.trace.json"))]
                if traces:
                    row["diagnostics"] = traces
            if row.get("type") == "round":
                streak = 0 if row.get("accepted") else streak + 1
                for component in counts:
                    counts[component] += any(p.startswith(component + "/") for p in row.get("changed", []))
        target = min(counts, key=counts.get) if streak >= self.search.stagnation else None
        number = self.state()["round"]
        return {"history": history, "search": {
            "edit_budget": self.search.budget(number), "stagnation": streak,
            "attempts": counts, "target": target,
            "prune": (number + 1) % self.search.prune_every == 0}}

    def trials(self, root, audit, label, seed, first=None):
        rows = []
        for i in range(self.search.trials):
            cache = None
            if self.search.cache_baseline and label == 'base-meta':
                # Initial baseline only: confirmation and candidate trials stay fresh.
                # Hash the environment, never write its credential values to disk.
                context = [identity(root), self.benchmark, self.datadesk_size, self.host_sandbox,
                           self.search.semantic_reviewer, seed + i, self.command,
                           self.task_command, self.timeout, sys.version,
                           sorted(os.environ.items()),
                           [(path.name, hashlib.sha256(path.read_bytes()).hexdigest())
                            for path in sorted((SOURCE / 'kernel').glob('*.py'))]]
                key = hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()
                cache = self.root / 'baseline-cache' / (key + '.json')
                if cache.exists():
                    cached = json.loads(cache.read_text())
                    atomic_json(audit / f'{label}-{i}.tasks.json', cached['evaluation'])
                    atomic_json(audit / f'{label}-{i}.cache.json', {'hit': True, 'key': key})
                    for suffix, value in cached['artifacts'].items():
                        if suffix in ('.trace.json', '.case.json', '.cost.json'):
                            atomic_json(audit / f'{label}-{i}{suffix}', value)
                    rows.append(cached['row'])
                    continue
            usage_path = self.root / 'usage.json'
            usage_before = json.loads(usage_path.read_text()) if usage_path.exists() else {'tokens':0, 'unknown_tokens':0}
            evaluation = first if i == 0 and first is not None else self.score(root, seed + i)
            atomic_json(audit / f'{label}-{i}.tasks.json', evaluation)
            row = [evaluation['score'], self.meta_probe(root, audit, f'{label}-{i}',
                                                       case_index=(seed % 10000) + i)]
            rows.append(row)
            usage_after = json.loads(usage_path.read_text()) if usage_path.exists() else usage_before
            tokens = (usage_after['tokens'] - usage_before['tokens']
                      if usage_after['unknown_tokens'] == usage_before['unknown_tokens'] and first is None else None)
            atomic_json(audit / f'{label}-{i}.cost.json', {'tokens':tokens})
            if cache is not None:
                cache.parent.mkdir(exist_ok=True)
                artifacts = {}
                for suffix in ('.trace.json', '.case.json', '.cost.json'):
                    path = audit / f'{label}-{i}{suffix}'
                    if path.exists():
                        artifacts[suffix] = json.loads(path.read_text())
                atomic_json(cache, {'evaluation': evaluation, 'row': row, 'artifacts': artifacts})
        return rows

    def accept_trials(self, candidate, baseline, audit, new_label, old_label, pruning=False, baseline_audit=None):
        def costs(label, directory):
            result = []
            for i in range(len(candidate)):
                path = directory / f'{label}-{i}.cost.json'
                result.append(json.loads(path.read_text()).get('tokens') if path.exists() else None)
            return result
        return confirmed_gain(candidate, baseline, self.search.noise_floor, pruning,
                              regression_tolerance=self.search.regression_tolerance,
                              candidate_costs=costs(new_label, audit),
                              baseline_costs=costs(old_label, baseline_audit or audit),
                              cost_band=self.search.cost_band)

    def score(self, root, seed, heldout=False):
        if self.benchmark == 'datadesk':
            return self.score_datadesk(root, seed, heldout)
        rows = []
        for task, expected in tasks(seed, suite=self.benchmark):
            try:
                answer = self.call(root, "solve", task=task)
                success = (isinstance(answer, (int, float)) and not isinstance(answer, bool)
                           and math.isfinite(answer) and abs(answer - expected) < 1e-8)
                rows.append({"task": task, "ok": success, "answer": answer})
            except ModelUnavailable:
                raise
            except Exception as error:
                rows.append({"task": task, "ok": False, "error": str(error)[-1000:]})
        return {"score": sum(row["ok"] for row in rows) / len(rows), "traces": rows}

    def score_datadesk(self, root, seed, heldout=False):
        if not self.task_command:
            raise ModelUnavailable('DataDesk requires a task model backend')
        if self.datadesk_size in ('pagination', 'pagination_extended'):
            return self.score_pagination(root, seed, heldout)
        with tempfile.TemporaryDirectory(prefix='rsi-datadesk-') as directory:
            directory = Path(directory)
            (directory / 'tasks').mkdir()
            hashes = {}
            families = EVOLVE + HELDOUT_ONLY if heldout else EVOLVE
            plan = [(f,1) for f in families]
            if self.datadesk_size == 'full':
                plan = ([(f,2) for f in EVOLVE] + [(f,5) for f in HELDOUT_ONLY] if heldout
                        else [(f,3) for f in EVOLVE])
            ids = make_split(directory, 'evaluation', 'd', plan, seed, hashes)
            metadata = json.loads((directory / 'tasks/evaluation.json').read_text())
            rows = []
            for tid in ids:
                item = metadata[tid]
                task = {'kind':'datadesk', 'id':tid, 'question':item['question'], 'type':item['type']}
                try:
                    session = self.datadesk_session(root, item['question'], directory / 'tasks/evaluation' / tid)
                    ok = session['stop_reason'] == 'answer' and check_answer(tid, item['type'], session['answer'], hashes[tid])
                    rows.append({'task':task, 'ok':ok, **session})
                except ModelUnavailable:
                    raise
                except Exception as error:
                    rows.append({'task':task, 'ok':False, 'error':str(error)[-1000:]})
            return self.semantic_review(root,diagnose_evaluation({'score':sum(row['ok'] for row in rows) / len(rows), 'traces':rows,
                    'split':'heldout' if heldout else 'search', 'seed':seed, 'families':families,
                    'size':self.datadesk_size}))

    def score_pagination(self, root, seed, heldout=False):
        # Same task families in both splits, disjoint data by fixed seed offset.
        data_seed = seed + 1000000007 if heldout else seed
        rows = []
        maker = (make_extended_pagination_tasks if self.datadesk_size == 'pagination_extended'
                 else make_pagination_tasks)
        with tempfile.TemporaryDirectory(prefix='rsi-pages-') as directory:
            for item in maker(Path(directory), data_seed):
                task = {key:item[key] for key in ('id','question','type')}
                task['kind'] = 'datadesk'
                try:
                    session = self.datadesk_session(root,item['question'],item['directory'])
                    ok = session['stop_reason']=='answer' and check_answer(item['id'],item['type'],session['answer'],item['digest'])
                    rows.append({'task':task,'ok':ok,**session})
                except ModelUnavailable:
                    raise
                except Exception as error:
                    rows.append({'task':task,'ok':False,'error':str(error)[-1000:]})
        return self.semantic_review(root,diagnose_evaluation({'score':sum(row['ok'] for row in rows)/len(rows),'traces':rows,
                                   'size':self.datadesk_size,'split':'heldout' if heldout else 'search',
                                   'seed':seed,'data_seed':data_seed,
                                   'scope':'CSV-only interface; heldout uses new data in the same task families'}))

    def semantic_review(self, root, evaluation):
        if self.search.semantic_reviewer:
            for trace in evaluation['traces']:
                if trace['ok']:
                    continue
                text = self.model_response(root,review_messages(trace),command=self.search.semantic_reviewer)
                try:
                    trace['semantic_review'] = validate_review(text,trace)
                except ValueError:
                    trace['semantic_review'] = {'error':'invalid reviewer response; no hypothesis accepted'}
        return evaluation

    def meta_probe(self, root, audit, label, case_index=0):
        # Exercise the generation's own policy on a separate code-repair task.
        # Scoring is done here, not by the candidate or the coding model.
        suite = 'advanced' if self.benchmark == 'datadesk' else self.benchmark
        return self.repair_probe(root, audit, label, repair_case(suite, case_index))

    def repair_probe(self, root, audit, label, case):
        """Score a supervisor-owned project without exposing hidden test values."""
        atomic_json(audit / (label + '.case.json'), {'name': case['name']})
        prompt = self.call(root, "plan", evidence={"failure": case['failure']}, goal=case['goal'])
        with tempfile.TemporaryDirectory(prefix="rsi-meta-") as directory:
            workspace = Path(directory)
            for name, source in case['files'].items():
                (workspace / name).write_text(source)
            protected = {name: (workspace / name).read_bytes() for name in case['files']
                         if name not in case['mutable']}
            try:
                self.code(workspace, prompt, audit / (label + ".log"))
            except TraceFailure:
                return 0.0
            if any(not (workspace / name).is_file() or (workspace / name).read_bytes() != value
                   for name, value in protected.items()):
                return 0.0
            files = manifest(workspace)
            if set(files) != set(case['files']):
                return 0.0
            # Fresh supervisor-only test values, including negatives and floats.
            program = ('import json\n' + case['imports'] + '\n' + case.get('setup', '') +
                       '\nprint(json.dumps(' + case['expression'] + '))')
            if self.host_sandbox in ('macos', 'linux-bwrap'):
                memory_cap = 536870912 if self.host_sandbox == 'linux-bwrap' else None
                command = sandbox_command([sys.executable,'-I','-B','-c',limited_python(
                    'import sys; sys.path.insert(0,'+repr(str(workspace))+');\n'+program,
                    memory_bytes=memory_cap)],readable=[workspace],backend=self.host_sandbox)
                result = run_bounded(command,'',workspace,30,1048576,env=clean_environment())
            else:
                result = subprocess.run([sys.executable,'-B','-c',program],cwd=workspace,
                                        capture_output=True,text=True,timeout=30)
            if result.returncode:
                return 0.0
            try:
                answers = json.loads(result.stdout)
                expected = case['expected']
                if not isinstance(answers, list) or len(answers) != len(expected):
                    return 0.0
                return sum(answers_equal(a, b) for a, b in zip(answers, expected)) / len(expected)
            except (ValueError, TypeError):
                return 0.0

    @staticmethod
    def better(new, old):
        # Neither task ability nor improvement ability may regress.
        return (new[0] >= old[0] and new[1] >= old[1]
                and (new[0] > old[0] or new[1] > old[1]))

    def round(self):
        state = self.recover(self.state())
        current = self.version(state["active"])
        number = state["round"]
        audit = self.root / "rounds" / f"{number:04d}-{uuid.uuid4().hex[:8]}"
        audit.mkdir(parents=True)
        old_trials = self.trials(current, audit, "base-meta", number + 10)
        old = json.loads((audit / 'base-meta-0.tasks.json').read_text())
        old_meta = sum(row[1] for row in old_trials) / len(old_trials)
        base_evaluations = [json.loads((audit / f'base-meta-{i}.tasks.json').read_text())
                            for i in range(len(old_trials))]
        evidence = {"task_evaluation": compact_evidence(old), "improvement_score": old_meta,
                    'trial_evaluation_summaries':[{'trial':i,'score':value['score']} for i,value in enumerate(base_evaluations)],
                    **self.search_evidence()}
        failures = task_failure_evidence(base_evaluations)
        if old_meta < 1:
            failures.append({"id": "policy-repair", "kind": "repair_failure", "score": old_meta})
        for trace_path in sorted(audit.glob("base-meta-*.trace.json")):
            trace = json.loads(trace_path.read_text())
            if trace["failures"]:
                failures.append({"id": "trace-" + trace_path.stem,
                                 "kind": "trace_failure", "trace": trace})
        failures.append({"id": "complexity", "kind": "maintenance",
                         "description": "Remove redundant mechanisms without losing capability"})
        evidence["failures"] = failures
        if evidence["search"]["prune"] and (self.root / "ablation.json").exists():
            try:
                ablation_report = json.loads((self.root / "ablation.json").read_text())
            except (OSError, ValueError):
                ablation_report = None
            hints = confirmed_ablation_hints(ablation_report, state["active"])
            if hints:
                evidence["ablation_hints"] = hints
        strategy = self.analyze(current, evidence)
        if strategy is not None:
            evidence['strategy'] = strategy
            atomic_json(audit / 'strategy.json', strategy)
        goal = ("Improve this system's task solver AND its self-improvement policy. "
                "Only agent/ and evolution/ may change. Preserve solve(task) and "
                "build_prompt(evidence, goal). Do not access the supervisor, its state, "
                "benchmarks or answers. The next generation will use YOUR updated "
                "evolution/policy.py to produce further improvements.")
        if self.task_command:
            goal += " Preserve build_messages(task) returning system/prompt strings and solve(task, llm=None)."
            if self.benchmark == 'datadesk':
                goal += " Preserve parse_action(text). DataDesk runs up to 10 actions: JSON {tool,args} or {answer}. build_messages receives question, tool descriptions and full action/result history. Tools and scoring are fixed."
                from .datadesk import TOOL_HELP
                goal += ' Available tool contracts for this profile: '+json.dumps(
                    PaginatedToolbox.help if self.datadesk_size in ('pagination', 'pagination_extended') else TOOL_HELP)
            else:
                goal += " The task runtime supports one stateless model call per task."
        prompt = self.call(current, "plan", evidence=evidence, goal=goal)
        evidence_block = '\nEvidence:\n' + json.dumps(evidence, ensure_ascii=False)
        if prompt.endswith(evidence_block):
            prompt = prompt[:-len(evidence_block)]
        prompt += ("\nSupervisor requirements: Read Evidence before editing. Write .rsi-proposal.json "
                   "with analysis (evidence and cause), and changes: a list of objects containing "
                   "files, failure_id, cause, expected_effect, validation. Each changed file must "
                   "reference a supplied failure ID. If Evidence.strategy exists, EVERY change.failure_id "
                   "must belong to Evidence.strategy.failure_ids, and at least one changed file must "
                   "be in its target component unless Evidence.search.target overrides it. Other failures "
                   "may be fixed incidentally but must not be cited as unselected failure IDs. "
                   "Obey Evidence.search edit_budget (added plus "
                   "removed lines), target component, and prune (net deletion if true). "
                   "Do not alter kernel, tests or Git metadata.\nEvidence:\n"
                   + json.dumps(evidence, ensure_ascii=False))
        if len(prompt) > 100_000:
            raise RuntimeError("complete history exceeds prompt budget; use a new experiment state")
        (audit / "proposal.txt").write_text(prompt)
        atomic_json(audit / "evidence.json", evidence)
        record = {"type": "round", "round": number, "parent": state["active"],
                  "planner_version": state["active"], "accepted": False,
                  "baseline": [sum(r[0] for r in old_trials) / len(old_trials), old_meta],
                  "audit": str(audit), "search": evidence["search"],
                  "workspace": "git-worktree" if self.search.worktrees else "copy",
                  "trials": {"baseline": old_trials}}
        attempt_records = []
        for attempt_index in range(self.search.candidates):
            attempt_audit = audit if self.search.candidates == 1 else audit / f"attempt-{attempt_index}"
            attempt_audit.mkdir(parents=True, exist_ok=True)
            attempt_record = {"attempt": attempt_index, "accepted": False}
            with candidate_workspace(current, self.search.worktrees) as candidate:
                try:
                    attempt_record['stage'] = 'coding'
                    git_control = (candidate / ".git").read_bytes() if self.search.worktrees else None
                    self.code(candidate, prompt, attempt_audit / "coder.log")
                    attempt_record['stage'] = 'proposal_validation'
                    if self.search.worktrees:
                        if (candidate / ".git").read_bytes() != git_control:
                            raise ValueError("candidate changed Git metadata")
                        (candidate / ".git").unlink()
                    proposal_path = candidate / ".rsi-proposal.json"
                    if not proposal_path.is_file() or proposal_path.is_symlink():
                        raise ValueError("proposal manifest .rsi-proposal.json is required")
                    if proposal_path.stat().st_size > 100_000:
                        raise ValueError("proposal too large")
                    proposal = json.loads(proposal_path.read_text())
                    proposal_path.unlink()
                    atomic_json(attempt_audit / "proposal.json", proposal)
                    self.validate_tree(candidate)
                    shutil.copytree(candidate, attempt_audit / "candidate",
                                    ignore=shutil.ignore_patterns("__pycache__"))
                    patch_text = diff_trees(current, candidate)
                    (attempt_audit / "changes.patch").write_text(patch_text)
                    stats = patch_stats(current, candidate)
                    attempt_record["changed"] = stats["files"]
                    review = review_proposal(proposal, stats, evidence)
                    # Cheap deterministic gates precede potentially paid model review.
                    self.health(candidate)
                    self.analyze(candidate, evidence)
                    if self.search.reviewer:
                        attempt_record['stage'] = 'review'
                        # The reviewer receives a disposable copy; any source mutation is rejected.
                        with tempfile.TemporaryDirectory(prefix="rsi-review-") as directory:
                            review_root = Path(directory) / "source"
                            shutil.copytree(candidate, review_root)
                            fingerprint = identity(review_root)
                            review_prompt = json.dumps({"instruction": "Review evidence and patch. Return one JSON line with approved (boolean) and reason. Do not modify files.",
                                                        "evidence": evidence, "proposal": proposal,
                                                        "patch": patch_text})
                            output = self.code(review_root, review_prompt, attempt_audit / "reviewer.log",
                                               command=self.search.reviewer)
                            if identity(review_root) != fingerprint:
                                raise ValueError("reviewer modified source")
                            verdicts = []
                            for line in output.splitlines():
                                try:
                                    value = json.loads(line)
                                except ValueError:
                                    continue
                                if isinstance(value, dict) and "approved" in value:
                                    verdicts.append(value)
                            if len(verdicts) != 1 or type(verdicts[0]["approved"]) is not bool:
                                raise ValueError("reviewer must return one boolean verdict")
                            review["independent"] = verdicts[0]
                            review["approved"] = verdicts[0]["approved"]
                    atomic_json(attempt_audit / "review.json", review)
                    if not review["approved"]:
                        raise ValueError("reviewer rejected: " + str(review["independent"].get("reason", "")))
                    candidate_id = identity(candidate)
                    if candidate_id == state["active"]:
                        raise ValueError("no source changes")
                    before = manifest(current)
                    after = manifest(candidate)
                    attempt_record["changed"] = [p for p in sorted(set(before) | set(after))
                                                 if before.get(p) != after.get(p)]
                    attempt_record['stage'] = 'candidate_evaluation'
                    new_trials = self.trials(candidate, attempt_audit, "candidate-meta", number + 10)
                    new = json.loads((attempt_audit / 'candidate-meta-0.tasks.json').read_text())
                    attempt_record["candidate"] = [sum(r[d] for r in new_trials) / len(new_trials) for d in range(2)]
                    attempt_record["trials"] = {"candidate": new_trials}
                    atomic_json(attempt_audit / "evaluation.json", new)
                    if self.accept_trials(new_trials, old_trials, attempt_audit, 'candidate-meta', 'base-meta',
                                          evidence['search']['prune'], baseline_audit=audit):
                        attempt_record['stage'] = 'confirmation'
                        base_confirm = self.trials(current, attempt_audit, "base-confirm-meta", number + 10010)
                        confirm = self.trials(candidate, attempt_audit, "confirm-meta", number + 10010)
                        attempt_record["trials"].update(confirmation=confirm, baseline_confirmation=base_confirm)
                        attempt_record["confirmation"] = [sum(r[d] for r in confirm) / len(confirm) for d in range(2)]
                        if self.accept_trials(confirm, base_confirm, attempt_audit, 'confirm-meta', 'base-confirm-meta',
                                              evidence['search']['prune']):
                            version_id = self.publish(candidate)
                            # Activation readiness is checked from the published generation.
                            self.health(self.version(version_id))
                            state.update(previous=state["active"], active=version_id)
                            attempt_record.update(accepted=True, active=version_id, stage='activated')
                    if not attempt_record["accepted"]:
                        attempt_record["reason"] = "no confirmed improvement without regression"
                except (ValueError, RuntimeError, subprocess.TimeoutExpired, OSError) as error:
                    attempt_record["reason"] = str(error)
                finally:
                    if not attempt_record['accepted']:
                        try:
                            attempt_record['archive'] = self.archive_failure(candidate, current, attempt_audit)
                        except OSError as error:
                            attempt_record['archive_error'] = str(error)
            attempt_records.append(attempt_record)
            if attempt_record["accepted"]:
                break
            if attempt_record.get("reason", "").startswith(("budget exhausted", "budget exceeded", "usage unknown")):
                break
        final = attempt_records[-1]
        for key in ("accepted", "active", "stage", "reason", "changed", "candidate", "confirmation", "archive", "archive_error"):
            if key in final:
                record[key] = final[key]
        if "trials" in final:
            record["trials"].update(final["trials"])
        if self.search.candidates > 1:
            record["attempts"] = attempt_records
        state["round"] += 1
        atomic_json(self.state_path, state)
        atomic_json(audit / "result.json", record)
        self.event(**record)
        self.report()
        return record

    def list_versions(self):
        """Return all published versions with integrity status (read-only)."""
        state = self.state()
        items = []
        for path in sorted(self.versions.iterdir()):
            if not path.is_dir() or len(path.name) != 64 or any(c not in "0123456789abcdef" for c in path.name):
                continue
            try:
                files = manifest(path)
                valid = identity(path) == path.name
            except (ValueError, OSError):
                files, valid = {}, False
            items.append({
                "version": path.name,
                "active": path.name == state.get("active"),
                "previous": path.name == state.get("previous"),
                "valid": valid,
                "files": len(files),
            })
        return {"active": state.get("active"), "previous": state.get("previous"), "versions": items}

    def rollback(self, version_id):
        """Manually activate a previously published version after hash and health checks."""
        state = self.state()
        target = self.version(version_id)
        if not target.is_dir():
            raise ValueError("unknown version: " + version_id)
        if identity(target) != version_id:
            raise ValueError("target version hash mismatch")
        self.health(target)
        old_active = state["active"]
        if old_active == version_id:
            return {"active": version_id, "previous": state.get("previous"), "changed": False}
        state.update(previous=old_active, active=version_id)
        atomic_json(self.state_path, state)
        self.event(type="manual_rollback", failed=old_active, active=version_id)
        self.report()
        return {"active": version_id, "previous": old_active, "changed": True}

    def report(self):
        write_markdown_report(self.root, self.state(), self.recent_history())

    def recent_history(self, limit=8):
        path = self.root / "history.jsonl"
        if not path.exists():
            return []
        lines = path.read_text().splitlines()
        return [json.loads(line) for line in (lines[-limit:] if limit is not None else lines)]

    def run(self, rounds):
        with self.lock():
            self.initialize()
            records = []
            for _ in range(rounds):
                if (self.root / "STOP").exists():
                    break
                record = self.round()
                records.append(record)
                print(json.dumps(record, ensure_ascii=False), flush=True)
            return records

    def solve(self, task):
        with self.lock():
            self.initialize()
            state = self.recover(self.state())
            return self.call(self.version(state["active"]), "solve", task=task)

    def validate_versions(self, seeds=3):
        """Compare initial and active versions on fresh inputs, outside search history.

        Same task families with different values: this is not a claim of transfer
        to unseen task types, and does not measure repair-policy generalization.
        Caller must hold the supervisor lock.
        """
        if type(seeds) is not int or not 1 <= seeds <= 20:
            raise ValueError('validation seeds must be in [1, 20]')
        state = self.recover(self.state())
        initialized = next(row for row in self.recent_history(limit=None)
                           if row.get('type') == 'initialized')
        baseline = self.version(initialized['active'])
        active = self.version(state['active'])
        rows = []
        for i in range(seeds):
            seed = 982451653 + i
            before, after = self.score(baseline, seed), self.score(active, seed)
            rows.append({'seed':seed, 'baseline':before['score'], 'active':after['score'],
                         'improved':sum(not a['ok'] and b['ok'] for a,b in zip(before['traces'],after['traces'])),
                         'regressed':sum(a['ok'] and not b['ok'] for a,b in zip(before['traces'],after['traces']))})
        result = {'baseline_version':initialized['active'], 'active_version':state['active'],
                  'benchmark':self.benchmark, 'seeds':rows,
                  'baseline_score':sum(r['baseline'] for r in rows) / len(rows),
                  'active_score':sum(r['active'] for r in rows) / len(rows),
                  'scope':'fresh inputs in existing task families; not policy or unseen-family validation'}
        atomic_json(self.root / 'validation.json', result)
        return result

    def validate_policy(self):
        """Compare repair prompts on fixed projects excluded from search trials.

        Caller holds the lock. Calls share the normal usage ledger and bounds;
        validation artifacts never enter proposal history or version selection.
        """
        if not self.command:
            raise ValueError('policy validation requires a coding command')
        state = self.recover(self.state())
        initial = next(row['active'] for row in self.recent_history(limit=None)
                       if row.get('type') == 'initialized')
        audit = self.root / 'policy-validation' / uuid.uuid4().hex
        audit.mkdir(parents=True)
        rows = []
        for index, case in enumerate(heldout_repair_cases()):
            row = {'case': case['name']}
            for label, version in [('baseline', initial), ('active', state['active'])]:
                row[label] = self.repair_probe(self.version(version), audit, f'{index}-{label}', case)
            rows.append(row)
        result = {'baseline_version': initial, 'active_version': state['active'], 'cases': rows,
                  'baseline_score': sum(row['baseline'] for row in rows) / len(rows),
                  'active_score': sum(row['active'] for row in rows) / len(rows),
                  'audit': str(audit),
                  'scope': 'fixed repair families excluded from search; one sample per version and project, not statistical evidence of general improvement'}
        atomic_json(audit / 'result.json', result)
        atomic_json(self.root / 'policy-validation.json', result)
        return result

    def ablate(self, specs, seeds=2, seed=982451653, confirm_seeds=0):
        """Paired task-only diagnostics. Caller holds the lock; never publishes."""
        validate_specs(specs)
        if type(seeds) is not int or not 1 <= seeds <= 5 or type(seed) is not int:
            raise ValueError('ablation requires 1 to 5 seeds and an integer initial seed')
        if type(confirm_seeds) is not int or confirm_seeds not in (0,2,3,4,5):
            raise ValueError('confirmation requires 0 or 2 to 5 seeds')
        state = self.recover(self.state())
        current = self.version(state['active'])
        audit = self.root / 'ablations' / uuid.uuid4().hex
        audit.mkdir(parents=True)
        report = {'active':state['active'], 'benchmark':self.benchmark, 'status':'running',
                  'specs':specs, 'variants':[], 'audit':str(audit),
                  'scope':'paired task scores only; not repair-policy evaluation, semantic proof or automatic deletion'}
        def usage():
            path = self.root / 'usage.json'
            return json.loads(path.read_text()) if path.exists() else {}
        try:
            with tempfile.TemporaryDirectory(prefix='rsi-ablation-') as directory:
                variants = []
                # Prepare and check all variants before any potentially paid call.
                for spec in specs:
                    candidate = Path(directory) / spec['name']
                    shutil.copytree(current, candidate)
                    apply_spec(candidate, spec)
                    self.validate_tree(candidate)
                    self.health(candidate)
                    variants.append(candidate)
                    shutil.copytree(candidate, audit / spec['name'])
                for index, (spec, candidate) in enumerate(zip(specs, variants)):
                    variant = {'name':spec['name'], 'version':identity(candidate), 'trials':[]}
                    report['variants'].append(variant)
                    for trial in range(seeds+confirm_seeds):
                        confirming = trial>=seeds
                        if confirming and not assess_pairs(variant['trials']):
                            break
                        trial_seed = seed+trial if not confirming else seed+10000+trial-seeds
                        pair = {'seed':trial_seed}
                        variant.setdefault('confirmation' if confirming else 'trials',[]).append(pair)
                        roles = [('baseline',current),('ablated',candidate)]
                        if (trial+index) % 2:
                            roles.reverse()
                        pair['order'] = [role for role, _ in roles]
                        scores = {}
                        for role, root in roles:
                            before = usage()
                            scores[role] = self.score(root, trial_seed)
                            pair[role] = {'score':scores[role]['score'], **usage_delta(before,usage())}
                            atomic_json(audit / f'{spec["name"]}-{trial}-{role}.json',scores[role])
                        a, b = scores['baseline']['traces'], scores['ablated']['traces']
                        if len(a) != len(b) or any(x['task'] != y['task'] for x,y in zip(a,b)):
                            raise ValueError('ablation task pairing mismatch')
                        pair['regressed'] = sum(x['ok'] and not y['ok'] for x,y in zip(a,b))
                        pair['improved'] = sum(not x['ok'] and y['ok'] for x,y in zip(a,b))
                        atomic_json(audit / 'result.json',report)
                    variant['observed_regressions'] = sum(p['regressed'] for p in
                        variant['trials']+variant.get('confirmation',[]))
                    variant['conclusion'] = ('regression_observed' if variant['observed_regressions'] else
                                             'no_regression_observed_not_proof_of_redundancy')
                    variant['confirmed_task_benefit'] = bool(confirm_seeds and
                        len(variant.get('confirmation',[]))==confirm_seeds and assess_pairs(variant['trials'])
                        and assess_pairs(variant['confirmation']))
                    variant['deletion_authorized'] = False
            report['status'] = 'complete'
            atomic_json(self.root / 'ablation.json', report)
        except Exception as error:
            report.update(status='failed', error=type(error).__name__)
            raise
        finally:
            atomic_json(audit / 'result.json',report)
        return report
