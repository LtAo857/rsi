"""Parallel stdlib unittest runner by test module (preserves full subprocess boundaries)."""

from concurrent.futures import ProcessPoolExecutor, as_completed
import io
import os
from pathlib import Path
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


def _run_module(module_name):
    started = time.monotonic()
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromName(module_name)
    stream = io.StringIO()
    runner = unittest.TextTestRunner(stream=stream, verbosity=1)
    result = runner.run(suite)
    return {
        "module": module_name,
        "tests": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "ok": result.wasSuccessful(),
        "seconds": round(time.monotonic() - started, 2),
        "output": stream.getvalue(),
    }


def main():
    tests_dir = ROOT / "tests"
    modules = sorted(f"tests.{p.stem}" for p in tests_dir.glob("test_*.py"))
    workers = min(len(modules), max(1, os.cpu_count() or 1))
    started = time.monotonic()
    results = []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_run_module, name): name for name in modules}
        for future in as_completed(futures):
            row = future.result()
            results.append(row)
            status = "OK" if row["ok"] else "FAIL"
            print(f"[{status}] {row['module']} ({row['tests']} tests, {row['seconds']}s)", flush=True)
            if not row["ok"]:
                print(row["output"], flush=True)
    total_tests = sum(r["tests"] for r in results)
    total_failures = sum(r["failures"] for r in results)
    total_errors = sum(r["errors"] for r in results)
    elapsed = round(time.monotonic() - started, 2)
    print(f"\nRan {total_tests} tests across {len(modules)} modules in {elapsed}s "
          f"(workers={workers}, failures={total_failures}, errors={total_errors})")
    sys.exit(0 if total_failures == 0 and total_errors == 0 else 1)


if __name__ == "__main__":
    main()
