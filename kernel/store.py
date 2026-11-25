"""Atomic persistence, file locking, source manifests, validation, and failure archiving."""

from contextlib import contextmanager
import difflib
import hashlib
import json
import os
from pathlib import Path

REQUIRED = ("agent/solver.py", "evolution/policy.py")


def atomic_json(path, data):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    os.replace(temporary, path)


@contextmanager
def file_lock(lock_path):
    # flock is released by the OS even if this supervisor crashes.
    import fcntl
    with Path(lock_path).open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("another rsi supervisor is running") from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


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


def validate_tree(candidate, required=REQUIRED):
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
    if not all(relative in files for relative in required):
        raise ValueError("required interfaces were removed")


def diff_trees(current, candidate):
    patch = []
    for relative in sorted(set(manifest(current)) | set(manifest(candidate))):
        before_file, after_file = current / relative, candidate / relative
        before_text = before_file.read_text().splitlines(True) if before_file.exists() else []
        after_text = after_file.read_text().splitlines(True) if after_file.exists() else []
        patch.extend(difflib.unified_diff(
            before_text, after_text,
            fromfile="before/" + relative, tofile="after/" + relative))
    return "".join(patch)


def archive_failure(candidate, current, audit):
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
