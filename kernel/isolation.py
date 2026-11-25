"""Optional host sandbox boundaries for generated Python, fail closed.

Supports macOS Seatbelt (`macos`) and Linux bubblewrap (`linux-bwrap`).
The Python runtime and fixed adapters are trusted. This is not a VM and does
not isolate arbitrary networked coders.
"""
import json
from pathlib import Path
import shutil
import sys


def clean_environment():
    return {'PATH':'/usr/bin:/bin', 'LANG':'en_US.UTF-8', 'PYTHONHASHSEED':'0'}


def linux_sandbox_command(command, readable=(), writable=(), bwrap_path=None, platform=None):
    target_platform = sys.platform if platform is None else platform
    executable = Path(bwrap_path) if bwrap_path else (Path(shutil.which('bwrap')) if shutil.which('bwrap') else None)
    if target_platform != 'linux' or executable is None or not executable.is_file():
        raise ValueError('Linux bwrap sandbox unavailable; refusing unsandboxed execution')
    args = [
        str(executable),
        '--unshare-all',
        '--die-with-parent',
        '--new-session',
        '--clearenv',
        '--setenv', 'PATH', '/usr/bin:/bin',
        '--setenv', 'LANG', 'en_US.UTF-8',
        '--setenv', 'PYTHONHASHSEED', '0',
        '--dev', '/dev',
        '--proc', '/proc',
        '--tmpfs', '/tmp',
    ]
    seen_ro = set()
    for base in (Path('/usr'), Path('/lib'), Path('/lib64'), Path('/bin'), Path(sys.base_prefix), *map(Path, readable)):
        resolved = str(base.resolve()) if base.exists() else str(base)
        if resolved in seen_ro:
            continue
        if base.exists() or base in map(Path, readable):
            seen_ro.add(resolved)
            args.extend(['--ro-bind', resolved, resolved])
    seen_rw = set()
    for target in map(Path, writable):
        resolved = str(target.resolve())
        if resolved not in seen_rw:
            seen_rw.add(resolved)
            args.extend(['--bind', resolved, resolved])
    args.append('--')
    args.extend(command)
    return args


def sandbox_command(command, readable=(), writable=(), backend='macos'):
    if backend == 'linux-bwrap':
        return linux_sandbox_command(command, readable=readable, writable=writable)
    if backend != 'macos':
        raise ValueError('unknown host sandbox backend: ' + str(backend))
    executable = Path('/usr/bin/sandbox-exec')
    if sys.platform != 'darwin' or not executable.is_file():
        raise ValueError('macOS sandbox unavailable; refusing unsandboxed execution')
    reads = [Path('/System'),Path('/usr/lib'),Path('/usr/share'),Path(sys.base_prefix),
             Path('/dev/null'),Path('/dev/random'),Path('/dev/urandom'),*map(Path,readable)]
    writes = list(map(Path,writable))
    def rules(paths):
        return ' '.join('(subpath '+json.dumps(str(p.resolve()),ensure_ascii=False)+')' for p in paths)
    profile = ('(version 1)(deny default)(allow process-exec process-fork)'
               '(allow file-read-metadata)(allow file-read* (literal "/") '+rules(reads+writes)+')'
               '(allow file-write* (literal "/dev/null") '+rules(writes)+')')
    return [str(executable),'-p',profile,*command]


def trusted_adapter(command, source):
    # Only the fixed structured DeepSeek adapter may keep network/credentials.
    # Reject arbitrary coding commands and networked CLI tools in this mode.
    if (len(command) < 4 or Path(command[0]).resolve() != Path(sys.executable).resolve()
            or command[1] != '-B' or Path(command[2]).resolve() != (source/'kernel/backends.py').resolve()
            or command[3] != 'deepseek'):
        raise ValueError('sandbox mode permits only the fixed DeepSeek adapters; custom/Codex commands are refused')


def limited_python(program, memory_bytes=None):
    prefix = ('import resource, sys; '
              'resource.setrlimit(resource.RLIMIT_CPU,(30,30)); '
              'resource.setrlimit(resource.RLIMIT_FSIZE,(8388608,8388608)); '
              'resource.setrlimit(resource.RLIMIT_NOFILE,(128,128)); ')
    if memory_bytes is not None:
        limit = int(memory_bytes)
        prefix += (f'resource.setrlimit(resource.RLIMIT_AS,({limit},{limit})) '
                   f'if sys.platform.startswith("linux") and hasattr(resource,"RLIMIT_AS") else None; ')
    return prefix + '\n' + program
