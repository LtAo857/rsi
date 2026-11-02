"""Verify real process handoff, mutation scope, independent scoring and recovery."""

import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from kernel.engine import Engine, SOURCE, identity, manifest


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rsi-tests-")
        self.addCleanup(self.temp.cleanup)
        command = [sys.executable, "-B", str(SOURCE / "examples/demo_coder.py")]
        self.engine = Engine(Path(self.temp.name) / "state", command, timeout=30)

    def run_rounds(self, count):
        with contextlib.redirect_stdout(io.StringIO()):
            return self.engine.run(count)

    def test_two_generations_and_new_policy_handoff(self):
        original_source = {name: manifest(SOURCE / name) for name in ("agent", "evolution")}
        first, second, third = self.run_rounds(3)
        self.assertTrue(first["accepted"])
        self.assertEqual(first["baseline"], [0.5, 0.0])
        self.assertEqual(first["candidate"], [1.0, 0.0])
        self.assertEqual(first["changed"], ["agent/solver.py"])
        self.assertTrue(second["accepted"])
        self.assertEqual(second["planner_version"], first["active"])
        self.assertEqual(second["changed"], ["evolution/policy.py"])
        self.assertEqual(second["confirmation"], [1.0, 1.0])
        self.assertEqual(third["planner_version"], second["active"])
        prompt = (Path(third["audit"]) / "proposal.txt").read_text()
        self.assertIn("Read the tests before editing", prompt)
        self.assertFalse(third["accepted"])
        self.assertEqual(self.engine.state()["active"], second["active"])
        self.assertEqual(original_source, {name: manifest(SOURCE / name)
                                         for name in ("agent", "evolution")})
        result = self.engine.solve({"operation": "sum", "observation":
                                    '```json\n{"values":[100,-7,9]}\n```'})
        self.assertEqual(result, 102)

    def test_restart_continues_accepted_generation(self):
        first = self.run_rounds(1)[0]
        replacement = Engine(self.engine.root, self.engine.command, timeout=30)
        with contextlib.redirect_stdout(io.StringIO()):
            second = replacement.run(1)[0]
        self.assertEqual(second["parent"], first["active"])
        self.assertEqual(second["round"], 1)
        self.assertTrue(second["accepted"])

    def test_rollback_when_active_version_corrupted(self):
        first, second = self.run_rounds(2)
        root = self.engine.version(second["active"])
        (root / "evolution/policy.py").write_text("raise RuntimeError('broken')\n")
        value = self.engine.solve({"operation": "count", "observation": '{"values":[1,2]}'})
        self.assertEqual(value, 2)
        self.assertEqual(self.engine.state()["active"], first["active"])
        self.assertEqual(self.engine.recent_history()[-1]["type"], "rollback")

    def test_scope_and_symlink_rejection(self):
        self.engine.initialize()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "agent").mkdir()
            (root / "evolution").mkdir()
            (root / "agent/solver.py").write_text("x=1")
            (root / "evolution/policy.py").write_text("x=1")
            (root / "kernel.py").write_text("x=1")
            with self.assertRaisesRegex(ValueError, "outside mutable scope"):
                self.engine.validate_tree(root)
            (root / "kernel.py").unlink()
            (root / "agent/link.py").symlink_to(SOURCE / "agent/solver.py")
            with self.assertRaisesRegex(ValueError, "symlinks"):
                self.engine.validate_tree(root)

    def test_crash_counts_as_failure(self):
        self.engine.initialize()
        root = self.engine.version(self.engine.state()["active"])
        (root / "agent/solver.py").write_text("def solve(task):\n    raise RuntimeError('failure')\n")
        score = self.engine.score(root, 42)
        self.assertEqual(score["score"], 0)
        self.assertEqual(len(score["traces"]), 12)

    def test_no_regression_or_equal_score_acceptance(self):
        self.assertFalse(Engine.better([1, 0], [0.5, 1]))
        self.assertFalse(Engine.better([1, 1], [1, 1]))
        self.assertTrue(Engine.better([1, 1], [1, 0]))

    def test_stop_resume_and_single_writer(self):
        self.engine.initialize()
        with self.engine.lock():
            with self.assertRaisesRegex(RuntimeError, "another rsi"):
                with self.engine.lock():
                    pass
            result = subprocess.run(
                [sys.executable, "-m", "kernel", "--state", str(self.engine.root), "stop"],
                cwd=SOURCE, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((self.engine.root / "STOP").exists())
        (self.engine.root / "STOP").touch()
        self.assertEqual(self.run_rounds(2), [])
        (self.engine.root / "STOP").unlink()
        self.assertTrue(self.run_rounds(1)[0]["accepted"])

    def test_timeout_retains_stable_version(self):
        state = self.engine.initialize()
        self.engine.command = [sys.executable, "-c", "import time; time.sleep(10)"]
        self.engine.timeout = 0.1
        with self.assertRaises(subprocess.TimeoutExpired):
            self.run_rounds(1)
        self.assertEqual(self.engine.state(), state)

    def test_reject_broken_candidate_and_preserve_parent(self):
        state = self.engine.initialize()
        self.engine.command = [sys.executable, "-c",
                               "from pathlib import Path; "
                               "p=Path('agent/solver.py'); "
                               "p.write_text('invalid syntax !') if p.exists() else None"]
        record = self.run_rounds(1)[0]
        self.assertFalse(record["accepted"])
        self.assertEqual(self.engine.state()["active"], state["active"])

    def test_coder_failure_does_not_advance_generation(self):
        state = self.engine.initialize()
        self.engine.command = [sys.executable, "-c", "raise SystemExit(7)"]
        with self.assertRaisesRegex(RuntimeError, "coding command failed"):
            self.run_rounds(1)
        self.assertEqual(self.engine.state(), state)

    def test_runtime_source_mutation_is_rejected(self):
        self.engine.initialize()
        root = self.engine.version(self.engine.state()["active"])
        (root / "evolution/policy.py").write_text(
            "from pathlib import Path\n"
            "def build_prompt(evidence, goal):\n"
            "    Path(__file__).write_text('# changed at runtime')\n"
            "    return 'hello'\n")
        with self.assertRaisesRegex(RuntimeError, "modified its source"):
            self.engine.call(root, "plan", evidence={}, goal="test")

    def test_heldout_does_not_enter_history_or_change_active(self):
        self.run_rounds(1)
        state = self.engine.state()
        history = (self.engine.root / "history.jsonl").read_bytes()
        result = subprocess.run(
            [sys.executable, "-m", "kernel", "--state", str(self.engine.root), "heldout"],
            cwd=SOURCE, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["evaluation"]["score"], 1.0)
        self.assertEqual(self.engine.state(), state)
        self.assertEqual((self.engine.root / "history.jsonl").read_bytes(), history)


if __name__ == "__main__":
    unittest.main()
