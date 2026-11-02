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

from .evaluation import REPAIR_SOURCE, REPAIR_TESTS, repair_goal, tasks

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
    def __init__(self, state_dir, command=None, timeout=300):
        self.root = Path(state_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.versions = self.root / "versions"
        self.versions.mkdir(exist_ok=True)
        self.state_path = self.root / "state.json"
        self.command = command
        self.timeout = timeout

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
        value = self.call(root, "solve", task={"operation": "sum",
                                              "observation": '{"values": [2, 3]}'})
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
        if not all(relative in files for relative in REQUIRED):
            raise ValueError("required interfaces were removed")

    def code(self, workspace, prompt, log):
        if not self.command:
            raise RuntimeError("a coding command is required; use --coder or --demo")
        process = subprocess.run(self.command, input=prompt, cwd=workspace,
                                 text=True, capture_output=True, timeout=self.timeout)
        log.write_text(process.stdout + "\n" + process.stderr)
        if process.returncode:
            raise RuntimeError(f"coding command failed ({process.returncode}); see {log}")

    def score(self, root, seed):
        rows = []
        for task, expected in tasks(seed):
            try:
                answer = self.call(root, "solve", task=task)
                success = (isinstance(answer, (int, float)) and not isinstance(answer, bool)
                           and math.isfinite(answer) and abs(answer - expected) < 1e-8)
                rows.append({"task": task, "ok": success, "answer": answer})
            except Exception as error:
                rows.append({"task": task, "ok": False, "error": str(error)[-1000:]})
        return {"score": sum(row["ok"] for row in rows) / len(rows), "traces": rows}

    def meta_probe(self, root, audit, label):
        # Exercise the generation's own policy on a separate code-repair task.
        # Scoring is done here, not by the candidate or the coding model.
        prompt = self.call(root, "plan", evidence={"failure": "total ignores later values"},
                           goal=repair_goal())
        with tempfile.TemporaryDirectory(prefix="rsi-meta-") as directory:
            workspace = Path(directory)
            (workspace / "solution.py").write_text(REPAIR_SOURCE)
            (workspace / "test_solution.py").write_text(REPAIR_TESTS)
            original_tests = (workspace / "test_solution.py").read_bytes()
            self.code(workspace, prompt, audit / (label + ".log"))
            if (workspace / "test_solution.py").read_bytes() != original_tests:
                return 0.0
            files = manifest(workspace)
            if set(files) != {"solution.py", "test_solution.py"}:
                return 0.0
            # Fresh supervisor-only test values, including negatives and floats.
            program = ("import json; from solution import total; "
                       "print(json.dumps([total(v) for v in "
                       "[[], [2,3], [-7,4,10], [0.5,1.25], [8]]]))")
            result = subprocess.run([sys.executable, "-B", "-c", program], cwd=workspace,
                                    capture_output=True, text=True, timeout=30)
            if result.returncode:
                return 0.0
            try:
                answers = json.loads(result.stdout)
                expected = [0, 5, 7, 1.75, 8]
                if not isinstance(answers, list) or len(answers) != len(expected):
                    return 0.0
                return sum(type(a) in (int, float) and a == b
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
        old = self.score(current, seed=number + 10)
        old_meta = self.meta_probe(current, audit, "base-meta")
        evidence = {"task_evaluation": old, "improvement_score": old_meta,
                    "history": self.recent_history()}
        goal = ("Improve this system's task solver AND its self-improvement policy. "
                "Only agent/ and evolution/ may change. Preserve solve(task) and "
                "build_prompt(evidence, goal). Do not access the supervisor, its state, "
                "benchmarks or answers. The next generation will use YOUR updated "
                "evolution/policy.py to produce further improvements.")
        prompt = self.call(current, "plan", evidence=evidence, goal=goal)
        (audit / "proposal.txt").write_text(prompt)
        atomic_json(audit / "evidence.json", evidence)
        record = {"type": "round", "round": number, "parent": state["active"],
                  "planner_version": state["active"], "accepted": False,
                  "baseline": [old["score"], old_meta], "audit": str(audit)}
        with tempfile.TemporaryDirectory(prefix="rsi-candidate-") as directory:
            candidate = Path(directory) / "source"
            shutil.copytree(current, candidate, ignore=shutil.ignore_patterns("__pycache__"))
            try:
                self.code(candidate, prompt, audit / "coder.log")
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
                self.health(candidate)
                candidate_id = identity(candidate)
                if candidate_id == state["active"]:
                    raise ValueError("no source changes")
                before = manifest(current)
                after = manifest(candidate)
                record["changed"] = [p for p in sorted(set(before) | set(after))
                                     if before.get(p) != after.get(p)]
                new = self.score(candidate, seed=number + 10)
                new_meta = self.meta_probe(candidate, audit, "candidate-meta")
                record["candidate"] = [new["score"], new_meta]
                atomic_json(audit / "evaluation.json", new)
                if self.better(record["candidate"], record["baseline"]):
                    # Confirmation uses fresh task inputs; no held-out selection.
                    base_confirm = self.score(current, seed=number + 10010)
                    confirm = self.score(candidate, seed=number + 10010)
                    confirm_meta = self.meta_probe(candidate, audit, "confirm-meta")
                    baseline_meta = self.meta_probe(current, audit, "base-confirm-meta")
                    record["confirmation"] = [confirm["score"], confirm_meta]
                    if self.better(record["confirmation"],
                                   [base_confirm["score"], baseline_meta]):
                        version_id = self.publish(candidate)
                        # Activation readiness is checked from the published generation.
                        self.health(self.version(version_id))
                        state.update(previous=state["active"], active=version_id)
                        record.update(accepted=True, active=version_id)
                if not record["accepted"]:
                    record["reason"] = "no confirmed improvement without regression"
            except (ValueError, RuntimeError, subprocess.TimeoutExpired, OSError) as error:
                record["reason"] = str(error)
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

    def recent_history(self):
        path = self.root / "history.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()[-8:]]

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
