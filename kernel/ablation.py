"""Explicit, individually disabled agent mechanisms; no automatic pruning."""

from pathlib import Path
import re
from .search import confirmed_gain


def validate_specs(specs):
    if not isinstance(specs, list) or not 1 <= len(specs) <= 5:
        raise ValueError('provide 1 to 5 ablation specifications')
    names = set()
    for spec in specs:
        if not isinstance(spec, dict) or set(spec) != {'name','path','before','after'}:
            raise ValueError('ablation requires name/path/before/after')
        name, path = spec['name'], spec['path']
        if not isinstance(name, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,40}', name) or name in names:
            raise ValueError('ablation names must be unique simple identifiers')
        names.add(name)
        if (not isinstance(path, str) or Path(path).is_absolute() or '..' in Path(path).parts
                or Path(path).parts[:1] != ('agent',) or Path(path).suffix not in ('.py','.json','.md')):
            raise ValueError('ablation may only change agent source files')
        before, after = spec['before'], spec['after']
        if (not isinstance(before, str) or not isinstance(after, str) or
                not before or len(before.encode()) > 200000 or len(after) >= len(before)):
            raise ValueError('ablation must explicitly shorten a nonempty literal block')


def apply_spec(root, spec):
    path = root / spec['path']
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError('ablation target must be an existing source file')
    source = path.read_text()
    if source.count(spec['before']) != 1:
        raise ValueError('ablation block must match exactly once: ' + spec['name'])
    path.write_text(source.replace(spec['before'], spec['after'], 1))


def usage_delta(before, after):
    return {'calls':after.get('calls',0)-before.get('calls',0),
            'tokens':(after.get('tokens',0)-before.get('tokens',0)
                      if after.get('unknown_tokens',0) == before.get('unknown_tokens',0) else None)}


def assess_pairs(pairs):
    if len(pairs)<2 or any(pair['regressed'] for pair in pairs):
        return False
    return confirmed_gain([[p['ablated']['score']]*2 for p in pairs],
                          [[p['baseline']['score']]*2 for p in pairs],.01,
                          candidate_costs=[p['ablated']['tokens'] for p in pairs],
                          baseline_costs=[p['baseline']['tokens'] for p in pairs])


def confirmed_ablation_hints(report, active_version):
    """Extract read-only pruning hints from a complete ablation of the active version."""
    if (not isinstance(report, dict) or report.get('status') != 'complete'
            or report.get('active') != active_version):
        return []
    specs = {spec['name']: spec for spec in report.get('specs', [])
             if isinstance(spec, dict) and isinstance(spec.get('name'), str)}
    hints = []
    for variant in report.get('variants', []):
        if (not isinstance(variant, dict) or not variant.get('confirmed_task_benefit')
                or variant.get('observed_regressions', 0) != 0):
            continue
        spec = specs.get(variant.get('name'))
        if not spec:
            continue
        hints.append({
            'name': spec['name'],
            'path': spec['path'],
            'before': spec['before'],
            'after': spec['after'],
            'confirmed_task_benefit': True,
            'deletion_authorized': False,
            'note': 'Advisory task-only ablation evidence; candidate still requires full paired evaluation and confirmation.',
        })
    return hints
