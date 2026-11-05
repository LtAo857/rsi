"""Bound local coding processes and their descendants while retaining partial output."""

import json
import os
import selectors
import signal
import subprocess
import tempfile
import time


class CallLimit(RuntimeError):
    def __init__(self, reason, stdout, stderr):
        super().__init__(reason)
        self.stdout, self.stderr = stdout, stderr


def run_bounded(command, prompt, cwd, timeout, max_output_bytes=None, max_tokens=None):
    """POSIX process-group cleanup. Token limits act on reported usage, not billing."""
    buffers = {'stdout': bytearray(), 'stderr': bytearray()}
    partial = bytearray()
    native_tokens = 0
    started = time.monotonic()
    with tempfile.TemporaryFile() as input_file:
        input_file.write(prompt.encode())
        input_file.seek(0)
        process = subprocess.Popen(command, stdin=input_file, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, cwd=cwd, start_new_session=True)
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, 'stdout')
                selector.register(process.stderr, selectors.EVENT_READ, 'stderr')
                while selector.get_map():
                    if time.monotonic() - started > timeout:
                        raise subprocess.TimeoutExpired(command, timeout)
                    for key, _ in selector.select(.05):
                        data = os.read(key.fileobj.fileno(), 65536)
                        if not data:
                            selector.unregister(key.fileobj)
                            continue
                        if max_output_bytes is not None:
                            room = max_output_bytes - sum(len(v) for v in buffers.values())
                            buffers[key.data].extend(data[:max(0, room)])
                            if len(data) > room:
                                raise CallLimit('call output byte limit exceeded', '', '')
                        else:
                            buffers[key.data].extend(data)
                        if key.data == 'stdout' and max_tokens is not None:
                            partial.extend(data)
                            while b'\n' in partial:
                                line, _, remainder = partial.partition(b'\n')
                                partial = bytearray(remainder)
                                try:
                                    event = json.loads(line)
                                except (ValueError, UnicodeError):
                                    continue
                                if not isinstance(event, dict):
                                    continue
                                kind = event.get('type')
                                usage = event.get('usage', {}) if kind == 'turn.completed' else event
                                if not isinstance(usage, dict):
                                    continue
                                inp, out = usage.get('input_tokens'), usage.get('output_tokens')
                                if kind in ('usage', 'turn.completed') and type(inp) is int and type(out) is int and min(inp, out) >= 0:
                                    if kind == 'turn.completed':
                                        native_tokens += inp + out
                                    reported = native_tokens if kind == 'turn.completed' else inp + out
                                    if reported > max_tokens:
                                        raise CallLimit('reported call token limit exceeded', '', '')
            remaining = timeout - (time.monotonic() - started)
            process.wait(timeout=max(.001, remaining))
            return subprocess.CompletedProcess(command, process.returncode,
                       buffers['stdout'].decode(errors='replace'), buffers['stderr'].decode(errors='replace'))
        except (CallLimit, subprocess.TimeoutExpired) as error:
            error.stdout = bytes(buffers['stdout']).decode(errors='replace')
            error.stderr = bytes(buffers['stderr']).decode(errors='replace')
            raise
        finally:
            # Children may outlive a wrapper even after its own successful exit.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            process.stdout.close()
            process.stderr.close()
