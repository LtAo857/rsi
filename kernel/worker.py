"""Fresh interpreter boundary: load exactly one selected generation.

This is process isolation, not a hostile-code security sandbox.
"""

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys


def load(root, relative):
    spec = importlib.util.spec_from_file_location("generation_module", root / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    request = json.load(sys.stdin)
    root = Path(request["root"]).resolve()
    sys.path.insert(0, str(root))
    with contextlib.redirect_stdout(io.StringIO()):
        if request["op"] == "solve":
            module = load(root, "agent/solver.py")
            if 'model_response' in request:
                called = [False]
                def llm(system, prompt):
                    if called[0]:
                        raise ValueError('only one stateless model call is supported per task')
                    called[0] = True
                    # The fixed runner supports exactly one stateless call per task.
                    return request['model_response']
                result = module.solve(request['task'], llm=llm)
            else:
                result = module.solve(request["task"])
        elif request['op'] == 'messages':
            result = load(root, 'agent/solver.py').build_messages(request['task'])
        elif request['op'] == 'action':
            result = load(root, 'agent/solver.py').parse_action(request['text'])
        elif request['op'] == 'analyze':
            policy = load(root, 'evolution/policy.py')
            result = policy.analyze(request['evidence']) if hasattr(policy, 'analyze') else None
        else:
            result = load(root, "evolution/policy.py").build_prompt(
                request["evidence"], request["goal"])
            if not isinstance(result, str) or not result.strip() or len(result) > 100_000:
                raise ValueError("policy must return a nonempty prompt under 100KB")
    print(json.dumps({"result": result}, allow_nan=False))


if __name__ == "__main__":
    main()
