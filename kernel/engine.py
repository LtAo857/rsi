"""Trusted ratchet: snapshots, evaluation, meta-probes, activation and recovery.

Inspired by agent-rsi's frozen evaluation, fresh confirmation and candidate
isolation. Mutable generations contain both the solver and proposal policy.
Only this supervisor can publish a new generation.
"""

from contextlib import contextmanager
import difflib
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
from .evaluation import repair_case, tasks

SOURCE = Path(__file__).resolve().parents[1]
WORKER = Path(__file__).with_name("worker.py")
REQUIRED = ("agent/solver.py", "evolution/policy.py")


def atomic_json(path, data):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    os.replace(temporary, path)


def manifest(root):
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("symlinks are not permitted in generations")
        if path.is_file() and "__pycache__" not in path.parts:
            result[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def identity(root):
    return hashlib.sha256(json.dumps(manifest(root), sort_keys=True).encode()).hexdigest()


class Engine:
    def __init__(self, state_dir, command=None, timeout=300, task_command=None, benchmark=None, **settings):
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
        saved_benchmark = saved.get('benchmark', 'basic') if saved else 'basic'
        self.benchmark = benchmark or saved_benchmark
        if self.benchmark not in ('basic', 'advanced'):
            raise ValueError('unknown benchmark suite')
        if (saved or self.state_path.exists()) and self.benchmark != saved_benchmark:
            raise ValueError('benchmark changed; use a new state directory')
        if saved is not None and task_command is not None and saved['task_command'] != task_command:
            raise ValueError('task model configuration changed; use a new state directory')
        if saved is None and task_command is not None and self.state_path.exists():
            raise ValueError('cannot add a task model to an existing experiment; use a new state directory')
        self.task_command = saved['task_command'] if saved is not None else task_command
        # Persist at initialization under the writer lock, not in a read-only constructor.


    @contextmanager
    def lock(self):
        # flock is released by the OS even if this supervisor crashes.
        import fcntl
        with (self.root / "LOCK").open("a") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("another rsi supervisor is running") from None
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

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
        if op == 'solve' and self.task_command and use_model:
            messages = self.call(root, 'messages', task=arguments['task'])
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
                                   logs / (uuid.uuid4().hex + '.log'), command=self.task_command)
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
            arguments['model_response'] = responses[0]
        before = identity(root)
        process = subprocess.run(
            [sys.executable, "-I", "-B", str(WORKER)],
            input=json.dumps({"root": str(root), "op": op, **arguments}),
            text=True, capture_output=True, cwd=root, timeout=min(self.timeout, 30))
        if identity(root) != before:
            raise RuntimeError("generation modified its source during execution")
        if process.returncode:
            raise RuntimeError(process.stderr[-2000:])
        return json.loads(process.stdout)["result"]

    def health(self, root):
        for relative in REQUIRED:
            if not (root / relative).is_file():
                raise ValueError("missing " + relative)
        self.call(root, "plan", evidence={}, goal="health check")
        value = self.call(root, "solve", use_model=False, task={"operation": "sum",
                                              "observation": '{"values": [2, 3]}'})
        if self.task_command:
            self.call(root, 'messages', task={'operation':'sum', 'observation':'{"values":[2,3]}'})
        if value != 5:
            raise ValueError("basic solver health check failed")

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
        if self.task_command or self.benchmark != 'basic':
            runtime = self.root / 'runtime.json'
            if runtime.exists() and json.loads(runtime.read_text())['task_command'] != self.task_command:
                raise ValueError('task model configuration changed; use a new state directory')
            if not runtime.exists():
                atomic_json(runtime, {'task_command':self.task_command, 'benchmark':self.benchmark})
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
        files = manifest(candidate)
        for relative in files:
            parts = Path(relative).parts
            if parts[0] not in ("agent", "evolution") or not relative.endswith((".py", ".md", ".json")):
                raise ValueError("outside mutable scope: " + relative)
            if (candidate / relative).stat().st_size > 200_000:
                raise ValueError("source file too large")
            if relative.endswith('.py'):
                try:
                    compile((candidate / relative).read_bytes(), relative, 'exec')
                except (SyntaxError, ValueError) as error:
                    raise ValueError('invalid Python source: ' + relative) from error
        if not all(relative in files for relative in REQUIRED):
            raise ValueError("required interfaces were removed")

    def archive_failure(self, candidate, current, audit):
        """Preserve bounded, untrusted source even when normal validation never ran.

        Never follow symlinks or copy Git control files. Incomplete archives are
        explicit: an invalid candidate must not prevent publishing its diagnosis.
        """
        destination = audit / 'rejected-source'
        destination.mkdir(exist_ok=True)
        saved, skipped, errors, patch = [], [], [], []
        remaining = 2_000_000
        paths = set()
        for folder in ('agent', 'evolution'):
            for root in (candidate, current):
                base = root / folder
                if base.is_dir() and not base.is_symlink():
                    paths.update(p.relative_to(root) for p in base.rglob('*') if not p.is_dir())
        paths.add(Path('.rsi-proposal.json'))
        for relative in sorted(paths):
            versions = []
            for root in (current, candidate):
                source = root / relative
                try:
                    if any((root / Path(*relative.parts[:i])).is_symlink()
                           for i in range(1, len(relative.parts) + 1)):
                        if root == candidate:
                            skipped.append({'path': str(relative), 'reason': 'symlink'})
                        versions.append(None)
                        continue
                    if not source.is_file():
                        versions.append(b'')
                        continue
                    with source.open('rb') as stream:
                        data = stream.read(200_001)
                    if len(data) > 200_000 or len(data) > remaining:
                        if root == candidate:
                            skipped.append({'path': str(relative), 'reason': 'size limit'})
                        versions.append(None)
                        continue
                    if root == candidate:
                        target = destination / relative
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(data)
                        remaining -= len(data)
                        saved.append(str(relative))
                    versions.append(data)
                except OSError as error:
                    errors.append({'path': str(relative), 'reason': str(error)})
                    versions.append(None)
            if all(value is not None for value in versions):
                patch.extend(difflib.unified_diff(
                    versions[0].decode(errors='replace').splitlines(True),
                    versions[1].decode(errors='replace').splitlines(True),
                    fromfile='before/' + str(relative), tofile='after/' + str(relative)))
        (audit / 'rejected-changes.patch').write_text(''.join(patch))
        metadata = {'saved': saved, 'skipped': skipped, 'errors': errors,
                    'complete': not skipped and not errors,
                    'scope': 'agent/, evolution/, proposal; excludes symlinks and Git metadata'}
        atomic_json(audit / 'rejected-archive.json', metadata)
        return metadata

    def code(self, workspace, prompt, log, command=None):
        command = command or self.command
        if not command:
            raise RuntimeError("a coding command is required; use --coder or --demo")
        if self.search.max_prompt_chars is not None and len(prompt) > self.search.max_prompt_chars:
            raise RuntimeError('call prompt character limit exceeded before launch')
        usage_path = self.root / "usage.json"
        usage = (json.loads(usage_path.read_text()) if usage_path.exists() else
                 {"calls": 0, "tokens": 0, "cost_usd": 0.0,
                  "unknown_tokens": 0, "unknown_cost": 0})
        for limit, key in ((self.search.max_calls, "calls"),
                           (self.search.max_tokens, "tokens"),
                           (self.search.max_cost, "cost_usd")):
            if limit is not None and usage[key] >= limit:
                raise RuntimeError("budget exhausted: " + key)
        if ((self.search.max_tokens is not None and usage["unknown_tokens"]) or
                (self.search.max_cost is not None and usage["unknown_cost"])):
            raise RuntimeError("usage unknown; cannot enforce configured budget")
        # Reserve before launching: a crash or timeout cannot erase a paid invocation.
        usage["calls"] += 1
        usage["unknown_tokens"] += 1
        usage["unknown_cost"] += 1
        atomic_json(usage_path, usage)
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
        for key, missing in (("tokens", "unknown_tokens"), ("cost_usd", "unknown_cost")):
            if trace[key] is not None:
                usage[key] += trace[key]
                usage[missing] -= 1
        atomic_json(usage_path, usage)
        if interrupted:
            raise interrupted
        if process.returncode:
            raise RuntimeError(f"coding command failed ({process.returncode}); see {log}")
        for limit, key in ((self.search.max_tokens, "tokens"), (self.search.max_cost, "cost_usd")):
            if limit is not None and trace[key] is None:
                raise RuntimeError("usage missing for configured budget: " + key)
            if limit is not None and usage[key] > limit:
                raise RuntimeError("budget exceeded: " + key)
        if self.search.max_call_tokens is not None and trace['tokens'] is not None and trace['tokens'] > self.search.max_call_tokens:
            raise RuntimeError('reported call token limit exceeded')
        if self.search.max_call_tokens is not None and trace['tokens'] is None:
            raise RuntimeError('usage missing for configured call token limit')
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
                context = [identity(root), self.benchmark, seed + i, self.command,
                           self.task_command, self.timeout, sys.version,
                           sorted(os.environ.items()),
                           [(name, hashlib.sha256((SOURCE / 'kernel' / name).read_bytes()).hexdigest())
                            for name in ('engine.py', 'evaluation.py', 'worker.py', 'backends.py', 'search.py')]]
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

    def accept_trials(self, candidate, baseline, audit, new_label, old_label, pruning=False):
        def costs(label):
            result = []
            for i in range(len(candidate)):
                path = audit / f'{label}-{i}.cost.json'
                result.append(json.loads(path.read_text()).get('tokens') if path.exists() else None)
            return result
        return confirmed_gain(candidate, baseline, self.search.noise_floor, pruning,
                              regression_tolerance=self.search.regression_tolerance,
                              candidate_costs=costs(new_label), baseline_costs=costs(old_label),
                              cost_band=self.search.cost_band)

    def score(self, root, seed):
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

    def meta_probe(self, root, audit, label, case_index=0):
        # Exercise the generation's own policy on a separate code-repair task.
        # Scoring is done here, not by the candidate or the coding model.
        case = repair_case(self.benchmark, case_index)
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
            program = 'import json; ' + case['imports'] + '; print(json.dumps(' + case['expression'] + '))'
            result = subprocess.run([sys.executable, "-B", "-c", program], cwd=workspace,
                                    capture_output=True, text=True, timeout=30)
            if result.returncode:
                return 0.0
            try:
                answers = json.loads(result.stdout)
                expected = case['expected']
                if not isinstance(answers, list) or len(answers) != len(expected):
                    return 0.0
                return sum(type(a) is type(b) and a == b or
                           type(a) in (int, float) and type(b) in (int, float) and a == b
                           for a, b in zip(answers, expected)) / len(expected)
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
        evidence = {"task_evaluation": old, "improvement_score": old_meta,
                    **self.search_evidence()}
        failures = []
        for index, row in enumerate(old["traces"]):
            if not row["ok"]:
                failures.append({"id": f"task-{index}", "kind": "task_failure", "trace": row})
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
        strategy = self.call(current, 'analyze', evidence=evidence)
        if strategy is not None:
            known = {row['id'] for row in failures}
            if (not isinstance(strategy, dict) or
                    not isinstance(strategy.get('failure_ids'), list) or
                    not strategy['failure_ids'] or
                    any(not isinstance(value, str) or value not in known for value in strategy['failure_ids']) or
                    strategy.get('target') not in ('agent', 'evolution') or
                    not isinstance(strategy.get('hypothesis'), str) or not strategy['hypothesis'].strip()):
                raise ValueError('invalid mutable analysis strategy')
            evidence['strategy'] = strategy
            atomic_json(audit / 'strategy.json', strategy)
        goal = ("Improve this system's task solver AND its self-improvement policy. "
                "Only agent/ and evolution/ may change. Preserve solve(task) and "
                "build_prompt(evidence, goal). Do not access the supervisor, its state, "
                "benchmarks or answers. The next generation will use YOUR updated "
                "evolution/policy.py to produce further improvements.")
        if self.task_command:
            goal += " Preserve build_messages(task) returning system/prompt strings and solve(task, llm=None). The task runtime supports one stateless model call per task."
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
        with candidate_workspace(current, self.search.worktrees) as candidate:
            try:
                record['stage'] = 'coding'
                git_control = (candidate / ".git").read_bytes() if self.search.worktrees else None
                self.code(candidate, prompt, audit / "coder.log")
                record['stage'] = 'proposal_validation'
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
                atomic_json(audit / "proposal.json", proposal)
                self.validate_tree(candidate)
                shutil.copytree(candidate, audit / "candidate",
                                ignore=shutil.ignore_patterns("__pycache__"))
                patch = []
                for relative in sorted(set(manifest(current)) | set(manifest(candidate))):
                    before_file, after_file = current / relative, candidate / relative
                    before_text = before_file.read_text().splitlines(True) if before_file.exists() else []
                    after_text = after_file.read_text().splitlines(True) if after_file.exists() else []
                    patch.extend(difflib.unified_diff(before_text, after_text,
                                 fromfile="before/" + relative, tofile="after/" + relative))
                (audit / "changes.patch").write_text("".join(patch))
                stats = patch_stats(current, candidate)
                record["changed"] = stats["files"]
                review = review_proposal(proposal, stats, evidence)
                # Cheap deterministic gates precede potentially paid model review.
                self.health(candidate)
                if self.search.reviewer:
                    record['stage'] = 'review'
                    # The reviewer receives a disposable copy; any source mutation is rejected.
                    with tempfile.TemporaryDirectory(prefix="rsi-review-") as directory:
                        review_root = Path(directory) / "source"
                        shutil.copytree(candidate, review_root)
                        fingerprint = identity(review_root)
                        review_prompt = json.dumps({"instruction": "Review evidence and patch. Return one JSON line with approved (boolean) and reason. Do not modify files.",
                                                    "evidence": evidence, "proposal": proposal,
                                                    "patch": "".join(patch)})
                        output = self.code(review_root, review_prompt, audit / "reviewer.log",
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
                atomic_json(audit / "review.json", review)
                if not review["approved"]:
                    raise ValueError("reviewer rejected: " + str(review["independent"].get("reason", "")))
                candidate_id = identity(candidate)
                if candidate_id == state["active"]:
                    raise ValueError("no source changes")
                before = manifest(current)
                after = manifest(candidate)
                record["changed"] = [p for p in sorted(set(before) | set(after))
                                     if before.get(p) != after.get(p)]
                record['stage'] = 'candidate_evaluation'
                new_trials = self.trials(candidate, audit, "candidate-meta", number + 10)
                new = json.loads((audit / 'candidate-meta-0.tasks.json').read_text())
                record["candidate"] = [sum(r[d] for r in new_trials) / len(new_trials) for d in range(2)]
                record["trials"]["candidate"] = new_trials
                atomic_json(audit / "evaluation.json", new)
                if self.accept_trials(new_trials, old_trials, audit, 'candidate-meta', 'base-meta', evidence['search']['prune']):
                    record['stage'] = 'confirmation'
                    base_confirm = self.trials(current, audit, "base-confirm-meta", number + 10010)
                    confirm = self.trials(candidate, audit, "confirm-meta", number + 10010)
                    record["trials"].update(confirmation=confirm, baseline_confirmation=base_confirm)
                    record["confirmation"] = [sum(r[d] for r in confirm) / len(confirm) for d in range(2)]
                    if self.accept_trials(confirm, base_confirm, audit, 'confirm-meta', 'base-confirm-meta', evidence['search']['prune']):
                        version_id = self.publish(candidate)
                        # Activation readiness is checked from the published generation.
                        self.health(self.version(version_id))
                        state.update(previous=state["active"], active=version_id)
                        record.update(accepted=True, active=version_id, stage='activated')
                if not record["accepted"]:
                    record["reason"] = "no confirmed improvement without regression"
            except (ValueError, RuntimeError, subprocess.TimeoutExpired, OSError) as error:
                record["reason"] = str(error)
            finally:
                if not record['accepted']:
                    try:
                        record['archive'] = self.archive_failure(candidate, current, audit)
                    except OSError as error:
                        record['archive_error'] = str(error)
        state["round"] += 1
        atomic_json(self.state_path, state)
        atomic_json(audit / "result.json", record)
        self.event(**record)
        self.report()
        return record

    def report(self):
        state = self.state()
        lines = ["# RSI 运行报告", "", f"当前版本：`{state['active']}`",
                 f"已完成轮数：{state['round']}", "",
                 "| 轮次 | 使用的策略版本 | 基线（任务/修复） | 候选 | 接受 |",
                 "|---|---|---|---|---|"]
        for record in self.recent_history():
            if record.get("type") == "round":
                lines.append(f"| {record['round']} | {record['planner_version'][:12]} | "
                             f"{record['baseline']} | {record.get('candidate', '未通过')} | "
                             f"{record['accepted']} |")
        lines += ["", "完整历史见 history.jsonl；每轮的证据、源码、diff 与日志见 rounds/。",
                  "离线 demo 使用预设补丁，分数仅验证生命周期，不证明模型自主改进能力。"]
        (self.root / "REPORT.md").write_text("\n".join(lines) + "\n")

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
