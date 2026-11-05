"""Frozen DeepSeek HTTP and Codex CLI adapters. Standard library only.

Runnable by absolute path from candidate workspaces. Keys stay in environment.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request


class ModelUnavailable(RuntimeError):
    """Provider failure, never evidence of candidate task incompetence."""


class ResponseTruncated(ModelUnavailable):
    def __init__(self, usage):
        super().__init__('DeepSeek response truncated at provider output limit; partial patch not applied')
        self.usage = usage


def json_value(text):
    text = text.strip()
    if text.startswith('```'):
        text = text.split('\n', 1)[1].rsplit('```', 1)[0].strip()
    return json.loads(text)


def deepseek_completion(prompt, model, base_url, api_key_env, timeout=300, max_output_tokens=None, thinking=None):
    key = os.environ.get(api_key_env)
    if not key:
        raise ModelUnavailable('missing API key environment variable: ' + api_key_env)
    if not model:
        raise ModelUnavailable('DeepSeek requires an explicit model or RSI_MODEL_NAME / DATADESK_MODEL_NAME')
    payload = {'model':model, 'messages':[
        {'role':'system', 'content':'Follow the supplied protocol. Return only the requested output.'},
        {'role':'user', 'content':prompt}], 'stream':False}
    if max_output_tokens is not None:
        if type(max_output_tokens) is not int or max_output_tokens < 1:
            raise ValueError('max_output_tokens must be positive')
        payload['max_tokens'] = max_output_tokens
    if thinking is not None:
        if thinking not in ('enabled', 'disabled'):
            raise ValueError('invalid thinking mode')
        payload['thinking'] = {'type':thinking}
    body = json.dumps(payload).encode()
    request = urllib.request.Request(base_url.rstrip('/') + '/chat/completions', data=body,
                                     headers={'Content-Type':'application/json',
                                              'Authorization':'Bearer ' + key})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read())
    except urllib.error.HTTPError as error:
        # Never echo response bodies or request headers: either can contain secrets.
        raise ModelUnavailable(f'DeepSeek HTTP {error.code}') from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        raise ModelUnavailable('DeepSeek transport or response failure') from None
    try:
        native = data.get('usage') or {}
        usage = {'type':'usage', 'input_tokens':native.get('prompt_tokens'),
                 'output_tokens':native.get('completion_tokens')}
        if data['choices'][0].get('finish_reason') == 'length':
            raise ResponseTruncated(usage)
        text = data['choices'][0]['message']['content']
        if not isinstance(text, str):
            raise ValueError()
        native = data.get('usage') or {}
        usage = {'type':'usage', 'input_tokens':native.get('prompt_tokens'),
                 'output_tokens':native.get('completion_tokens')}
        # USD billing is not reported by chat completions; never fabricate zero cost.
        return text, usage
    except (KeyError, IndexError, TypeError, ValueError):
        raise ModelUnavailable('DeepSeek response missing text') from None


def codex_environment():
    """Bridge OS proxy settings into CLI env without overriding explicit choices."""
    environment = os.environ.copy()
    explicit = ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY',
                'http_proxy', 'https_proxy', 'all_proxy')
    if any(name in environment for name in explicit):
        return environment
    # urllib discovers macOS System Configuration / Windows registry proxies too.
    proxies = urllib.request.getproxies()
    # CLI transports may consult ALL_PROXY independently of the HTTP client.
    proxies.setdefault('all', proxies.get('https') or proxies.get('http'))
    for scheme, name in (('http', 'HTTP_PROXY'), ('https', 'HTTPS_PROXY'),
                         ('all', 'ALL_PROXY'), ('no', 'NO_PROXY')):
        if proxies.get(scheme) and name not in environment and name.lower() not in environment:
            environment[name] = proxies[scheme]
    return environment


def codex_completion(prompt, model='', timeout=300, workspace=None, events=None, isolated=False):
    with tempfile.TemporaryDirectory(prefix='rsi-codex-') as directory:
        output = Path(directory) / 'last.txt'
        command = ['codex', 'exec', '--skip-git-repo-check', '--ephemeral', '--json',
                   '--color', 'never', '-s', 'workspace-write' if workspace else 'read-only',
                   '-C', str(workspace or directory), '-o', str(output)]
        if model:
            command += ['-m', model]
        if isolated:
            command += ['--ignore-user-config']
        timed_out = False
        try:
            result = subprocess.run(command + ['-'], input=prompt, capture_output=True,
                                    text=True, timeout=timeout, env=codex_environment())
        except subprocess.TimeoutExpired as error:
            timed_out = True
            def decoded(value):
                return value.decode(errors='replace') if isinstance(value, bytes) else (value or '')
            result = subprocess.CompletedProcess(command, -1, decoded(error.stdout), decoded(error.stderr))
        except OSError:
            raise ModelUnavailable('Codex launch failure') from None
        totals = {'type':'usage', 'input_tokens':None, 'output_tokens':None}
        for line in result.stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get('type') == 'turn.completed':
                usage = event.get('usage') or {}
                inp, out = usage.get('input_tokens'), usage.get('output_tokens')
                if type(inp) is int and type(out) is int and min(inp, out) >= 0:
                    totals['input_tokens'] = (totals['input_tokens'] or 0) + inp
                    totals['output_tokens'] = (totals['output_tokens'] or 0) + out
            if events is not None:
                # Retain native diagnostic events in the calling supervisor's log.
                events.append(event)
        if result.returncode:
            if events is not None:
                events.append(totals)
            if timed_out:
                raise ModelUnavailable('Codex timeout; partial native events and reported usage retained')
            raise ModelUnavailable(f'Codex exit {result.returncode}; check login, quota and CLI configuration')
        if not output.exists():
            if events is not None:
                events.append(totals)
            raise ModelUnavailable('Codex returned no final message')
        return output.read_text(), totals


def apply_patch_response(root, response):
    """Apply a structured response only after validating every target path."""
    if not isinstance(response, dict) or not isinstance(response.get('files'), dict):
        raise ValueError('coder response requires files object')
    root = root.resolve()
    repair = (root / 'solution.py').exists()
    writes = []
    for name, content in response['files'].items():
        path = Path(name)
        allowed = (name == 'solution.py' or name == 'helpers.py' and (root / 'helpers.py').is_file() if repair else
                   bool(path.parts) and path.parts[0] in ('agent', 'evolution')
                   and path.suffix in ('.py', '.md', '.json'))
        if (path.is_absolute() or '..' in path.parts or not allowed or
                not (root / path).resolve().is_relative_to(root) or
                any((root / Path(*path.parts[:i])).is_symlink() for i in range(1, len(path.parts)+1))):
            raise ValueError('coder path outside mutable scope')
        if content is not None and (not isinstance(content, str) or len(content.encode()) > 200_000):
            raise ValueError('invalid coder file content')
        writes.append((root / path, content))
    if not repair and not isinstance(response.get('proposal'), dict):
        raise ValueError('coder response requires proposal manifest')
    for path, content in writes:
        if content is None:
            path.unlink(missing_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
    if not repair:
        manifest = root / '.rsi-proposal.json'
        if manifest.is_symlink():
            raise ValueError('proposal symlink is not permitted')
        manifest.write_text(json.dumps(response['proposal'], ensure_ascii=False))


def backend_command(backend, role, model='', base_url=None, api_key_env=None, timeout=300, codex_isolated=False,
                    max_output_tokens=None, thinking=None):
    model = model or os.environ.get('RSI_MODEL_NAME', os.environ.get('DATADESK_MODEL_NAME', ''))
    base_url = base_url or os.environ.get('RSI_BASE_URL', os.environ.get('DATADESK_BASE_URL', 'https://api.deepseek.com/v1'))
    api_key_env = api_key_env or os.environ.get('RSI_API_KEY_ENV', os.environ.get('DATADESK_API_KEY_ENV', 'DEEPSEEK_API_KEY'))
    command = [sys.executable, '-B', str(Path(__file__).resolve()), backend,
               '--role', role, '--timeout', str(timeout)]
    if model:
        command += ['--model', model]
    if base_url:
        command += ['--base-url', base_url]
    if api_key_env:
        command += ['--api-key-env', api_key_env]
    if backend == 'codex' and codex_isolated:
        command += ['--isolated']
    if max_output_tokens is not None:
        if backend != 'deepseek':
            raise ValueError('provider output token cap is currently supported only for DeepSeek')
        command += ['--max-output-tokens', str(max_output_tokens)]
    if thinking is not None:
        if backend != 'deepseek':
            raise ValueError('thinking mode is currently supported only for DeepSeek')
        command += ['--thinking', thinking]
    return command


def main():
    parser = argparse.ArgumentParser(description='RSI fixed model adapter')
    parser.add_argument('backend', choices=['deepseek', 'codex'])
    parser.add_argument('--role', choices=['task', 'coder', 'reviewer'], required=True)
    parser.add_argument('--model', default=os.environ.get('RSI_MODEL_NAME', os.environ.get('DATADESK_MODEL_NAME', '')))
    parser.add_argument('--base-url', default=os.environ.get('RSI_BASE_URL', os.environ.get('DATADESK_BASE_URL', 'https://api.deepseek.com/v1')))
    parser.add_argument('--api-key-env', default=os.environ.get('RSI_API_KEY_ENV', os.environ.get('DATADESK_API_KEY_ENV', 'DEEPSEEK_API_KEY')))
    parser.add_argument('--timeout', type=float, default=300)
    parser.add_argument('--isolated', action='store_true', help='Codex: ignore user config while preserving login')
    parser.add_argument('--max-output-tokens', type=int)
    parser.add_argument('--thinking', choices=['enabled', 'disabled'])
    args = parser.parse_args()
    prompt = sys.stdin.read()
    events = []
    usage = None
    try:
        if args.role == 'task':
            messages = json_value(prompt)
            prompt = messages['system'] + '\n\n' + messages['prompt']
        if args.role == 'coder' and args.backend == 'deepseek':
            root = Path.cwd()
            names = ([Path('solution.py'), Path('helpers.py'), Path('test_solution.py')] if (root / 'solution.py').exists()
                     else [p for folder in ('agent', 'evolution') for p in Path(folder).rglob('*')
                           if p.is_file() and not p.is_symlink() and '__pycache__' not in p.parts])
            sources = {str(p):p.read_text() for p in names if p.exists()}
            prompt += ('\nReturn one JSON object: {"files":{"relative/path":"complete new source or null to delete"},'
                       '"proposal":{"analysis":"evidence analysis","changes":[]}}. '
                       'List only changed files. In repair tasks solution.py and an existing helpers.py may change; preserve tests and no proposal is needed. '
                       'For candidate patches proposal must follow supervisor requirements. Current files:\n'
                       + json.dumps(sources, ensure_ascii=False))
        if args.backend == 'deepseek':
            text, usage = deepseek_completion(prompt, args.model, args.base_url, args.api_key_env, args.timeout,
                                             args.max_output_tokens, args.thinking)
        else:
            text, usage = codex_completion(prompt, args.model, args.timeout,
                                           Path.cwd() if args.role == 'coder' else None, events, args.isolated)
        if args.role == 'coder':
            if args.backend == 'deepseek':
                apply_patch_response(Path.cwd(), json_value(text))
        elif args.role == 'reviewer':
            verdict = json_value(text)
            if not isinstance(verdict, dict) or type(verdict.get('approved')) is not bool:
                raise ValueError('reviewer response must contain boolean approved')
            print(json.dumps(verdict, ensure_ascii=False))
        else:
            print(json.dumps({'model_response':text}, ensure_ascii=False))
    except ModelUnavailable as error:
        if isinstance(error, ResponseTruncated):
            usage = error.usage
        print('model unavailable: ' + str(error), file=sys.stderr)
        return 1
    except (ValueError, OSError, TypeError, KeyError):
        # Do not include prompts, API bodies, or provider stderr in error text.
        print('model unavailable or invalid adapter response; check configuration and provider status', file=sys.stderr)
        return 1
    finally:
        for event in events:
            print(json.dumps(event, ensure_ascii=False))
        if usage is not None:
            print(json.dumps(usage))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
