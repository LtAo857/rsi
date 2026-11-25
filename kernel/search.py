"""Supervisor-owned regularization. No model calls or mutable scoring rules."""

from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
import difflib
import json
import math
from pathlib import Path
import statistics
import subprocess
import tempfile


class TraceFailure(RuntimeError):
    """Behavioral failure of the agent, distinct from infrastructure or budget errors."""


@dataclass(frozen=True)
class SearchSettings:
    trials: int = 2
    noise_floor: float = .01
    edit_budget: int = 200
    min_edit_budget: int = 20
    decay: float = .9
    stagnation: int = 2
    prune_every: int = 5
    max_calls: int | None = None
    max_tokens: int | None = None
    max_cost: float | None = None
    reviewer: list | None = None
    worktrees: bool = False
    cache_baseline: bool = False
    regression_tolerance: float = 0.0
    cost_band: float = .05
    max_call_tokens: int | None = None
    max_prompt_chars: int | None = None
    max_output_bytes: int | None = None
    semantic_reviewer: list | None = None
    candidates: int = 1

    def __post_init__(self):
        for name in ('trials', 'edit_budget', 'min_edit_budget', 'stagnation', 'prune_every', 'candidates'):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(name + ' must be a positive integer')
        if self.candidates > 5:
            raise ValueError('candidates must be in [1, 5]')
        if self.min_edit_budget > self.edit_budget:
            raise ValueError('min_edit_budget exceeds edit_budget')
        if not math.isfinite(self.decay) or not 0 < self.decay <= 1:
            raise ValueError('decay must be in (0, 1]')
        if not math.isfinite(self.noise_floor) or not 0 <= self.noise_floor <= 1:
            raise ValueError('noise_floor must be in [0, 1]')
        for name in ('regression_tolerance', 'cost_band'):
            if not math.isfinite(getattr(self, name)) or not 0 <= getattr(self, name) < 1:
                raise ValueError(name + ' must be in [0, 1)')
        for name in ('max_calls', 'max_tokens', 'max_cost', 'max_call_tokens',
                     'max_prompt_chars', 'max_output_bytes'):
            value = getattr(self, name)
            if value is not None and (not math.isfinite(value) or value <= 0):
                raise ValueError(name + ' must be positive and finite')

    def budget(self, round_number):
        return max(self.min_edit_budget, int(self.edit_budget * self.decay ** round_number))


def confirmed_gain(candidate, baseline, noise_floor, pruning=False,
                   regression_tolerance=0.0, candidate_costs=None, baseline_costs=None, cost_band=.05):
    """Paired normal-approximation margin, not a formal small-sample guarantee."""
    if len(candidate) != len(baseline) or not candidate:
        return False
    gains = []
    for dimension in range(2):
        differences = [a[dimension] - b[dimension] for a, b in zip(candidate, baseline)]
        mean = statistics.mean(differences)
        # Preserve the strict no-observed-regression ratchet in either dimension.
        if regression_tolerance == 0 and min(differences) < 0:
            return False
        margin = (1.96 * statistics.stdev(differences) / math.sqrt(len(differences))
                  if len(differences) > 1 else 0)
        if regression_tolerance and mean - margin < -regression_tolerance:
            return False
        gains.append(mean > max(noise_floor, margin))
    cheaper = False
    if (candidate_costs is not None and baseline_costs is not None
            and len(candidate_costs) == len(baseline_costs) == len(candidate)
            and all(type(v) in (int, float) and math.isfinite(v) and v > 0
                    for v in candidate_costs + baseline_costs)):
        savings = [(old - new) / old for old, new in zip(baseline_costs, candidate_costs)]
        margin = (1.96 * statistics.stdev(savings) / math.sqrt(len(savings))
                  if len(savings) > 1 else 0)
        cheaper = statistics.mean(savings) > max(cost_band, margin)
    return pruning or any(gains) or cheaper


def inspect_trace(output):
    """Normalize an explicit JSONL adapter protocol; free text is not semantic evidence."""
    failures = set()
    calls = Counter()
    tokens = cost = None
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get('type')
        if kind in ('tool_error', 'give_up', 'loop'):
            failures.add(kind)
        if kind == 'tool_call':
            key = json.dumps([event.get('name'), event.get('arguments')], sort_keys=True)
            calls[key] += 1
            if calls[key] >= 3:
                failures.add('repeated_tool_call')
        if kind == 'usage':
            # Exactly one final cumulative usage event is expected per invocation.
            inp, out = event.get('input_tokens'), event.get('output_tokens')
            if type(inp) is int and type(out) is int and min(inp, out) >= 0:
                tokens = inp + out
            value = event.get('cost_usd')
            if type(value) in (int, float) and math.isfinite(value) and value >= 0:
                cost = value
    return {'failures': sorted(failures), 'tokens': tokens, 'cost_usd': cost}


def patch_stats(before, after):
    changed, lines, removed, added = [], 0, 0, 0
    paths = {p.relative_to(root).as_posix() for root in (before, after)
             for p in root.rglob('*') if p.is_file() and '.git' not in p.parts
             and '__pycache__' not in p.parts}
    for name in sorted(paths):
        old, new = before / name, after / name
        a = old.read_text().splitlines() if old.exists() else []
        b = new.read_text().splitlines() if new.exists() else []
        if old.exists() and new.exists() and old.read_bytes() == new.read_bytes():
            continue
        changed.append(name)
        for tag, i, j, k, l in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
            if tag != 'equal':
                removed += j - i
                added += l - k
                lines += j - i + l - k
    return {'files': changed, 'lines': lines, 'removed': removed, 'added': added}


def review_proposal(proposal, stats, evidence):
    """Validate claims' coverage and evidence references, not their semantic truth."""
    if not isinstance(proposal, dict) or not isinstance(proposal.get('analysis'), str) or not proposal['analysis'].strip():
        raise ValueError('proposal requires evidence analysis')
    changes = proposal.get('changes')
    if not isinstance(changes, list) or not changes:
        raise ValueError('proposal requires changes linked to failure modes')
    known = {row['id'] for row in evidence['failures']}
    covered = set()
    for change in changes:
        if not isinstance(change, dict):
            raise ValueError('invalid proposal change')
        for field in ('failure_id', 'cause', 'expected_effect', 'validation'):
            if not isinstance(change.get(field), str) or not change[field].strip():
                raise ValueError('proposal missing ' + field)
        if change['failure_id'] not in known:
            raise ValueError('proposal references unknown failure')
        if evidence.get('strategy') and change['failure_id'] not in evidence['strategy']['failure_ids']:
            raise ValueError('proposal did not follow mutable strategy failure selection')
        files = change.get('files')
        if not isinstance(files, list) or not files or any(not isinstance(p, str) for p in files):
            raise ValueError('proposal requires changed files')
        if not set(files) <= set(stats['files']):
            raise ValueError('proposal references unchanged files')
        covered.update(files)
    if covered != set(stats['files']):
        raise ValueError('proposal does not explain every changed file')
    search = evidence['search']
    if stats['lines'] > search['edit_budget']:
        raise ValueError('edit budget exceeded')
    target = search['target']
    if not target and evidence.get('strategy'):
        target = evidence['strategy']['target']
    if target and not any(p.startswith(target + '/') for p in stats['files']):
        raise ValueError('stagnation requires exploring component ' + target)
    if search['prune'] and stats['removed'] <= stats['added']:
        raise ValueError('pruning round requires a net deletion with evidence')
    return {'approved': True, 'stats': stats, 'checks': ['evidence_links', 'coverage', 'budget', 'exploration', 'pruning']}


@contextmanager
def candidate_workspace(current, worktrees=False):
    """A local disposable Git repository contains only mutable source, never kernel."""
    import shutil
    with tempfile.TemporaryDirectory(prefix='rsi-candidate-') as directory:
        base = Path(directory)
        candidate = base / 'source'
        if not worktrees:
            shutil.copytree(current, candidate, ignore=shutil.ignore_patterns('__pycache__'))
            yield candidate
            return
        repo = base / 'repo'
        shutil.copytree(current, repo, ignore=shutil.ignore_patterns('__pycache__'))

        def git(*args):
            result = subprocess.run(['git', '-C', str(repo), *args], capture_output=True,
                                    text=True, timeout=30)
            if result.returncode:
                raise RuntimeError('worktree setup failed: ' + result.stderr)
        git('init', '-q')
        git('add', '.')
        git('-c', 'user.name=RSI', '-c', 'user.email=rsi@localhost',
            '-c', 'commit.gpgsign=false', 'commit', '-qm', 'mutable generation')
        git('worktree', 'add', '--detach', str(candidate), 'HEAD')
        # .git is a worktree control file, excluded from candidate manifests only here.
        control = (candidate / '.git').read_bytes()
        try:
            yield candidate
        finally:
            (candidate / '.git').write_bytes(control)
            git('worktree', 'remove', '--force', str(candidate))
