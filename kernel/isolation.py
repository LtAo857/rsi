"""Optional macOS Seatbelt boundary for generated Python, fail closed.

The Python runtime and fixed adapters are trusted. This is not a VM, and does
not provide a portable memory quota or isolate arbitrary networked coders.
"""
import json
from pathlib import Path
import sys


def clean_environment():
    return {'PATH':'/usr/bin:/bin', 'LANG':'en_US.UTF-8', 'PYTHONHASHSEED':'0'}


def sandbox_command(command, readable=(), writable=()):
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


def limited_python(program):
    return ('import resource; '
            'resource.setrlimit(resource.RLIMIT_CPU,(30,30)); '
            'resource.setrlimit(resource.RLIMIT_FSIZE,(8388608,8388608)); '
            'resource.setrlimit(resource.RLIMIT_NOFILE,(128,128));\n'+program)
