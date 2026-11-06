"""Fixed DataDesk tools and bounded action loop, using only the standard library.

Tool restrictions protect the data interface, not the host from hostile Python.
"""

import csv
from decimal import Decimal, DecimalException
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from .datadesk_tasks import norm


TOOL_HELP = {
    'list_files': 'args {}: list data files with sizes',
    'read_file': 'args {path, offset=0, limit=4096}: read text characters, max 8192; returns next_offset/null',
    'read_csv': 'args {path, offset=0, limit=20}: CSV records (header excluded from offset), max 100 rows; returns next_offset/null',
    'schema': 'args {}: SQLite tables and columns; CSV and JSON-array files imported under filename stems, all columns TEXT',
    'sql': 'args {query}: one read-only SQLite SELECT, at most 100 result rows; use CAST for numeric sorting; prefer aggregate queries',
}


class Toolbox:
    def __init__(self, data_dir):
        self.root = Path(data_dir).resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError('data_dir must be a directory')
        self.db = sqlite3.connect(':memory:')
        self.tables = {}
        try:
            for path in sorted(self.root.iterdir()):
                if path.suffix not in ('.csv', '.json') or not path.is_file():
                    continue
                path = self.path(path.name)
                if path.stat().st_size > 2_000_000:
                    raise ValueError('data file exceeds 2 MB')
                if not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*', path.stem):
                    raise ValueError('invalid table name')
                with path.open(encoding='utf-8', newline='') as stream:
                    if path.suffix == '.csv':
                        reader = csv.DictReader(stream)
                        columns, rows = reader.fieldnames, list(reader)
                    else:
                        rows = json.load(stream)
                        if not isinstance(rows, list) or not rows or not all(isinstance(row, dict) for row in rows):
                            raise ValueError('JSON data must be a nonempty array of objects')
                        columns = list(rows[0])
                if (not columns or len(set(columns)) != len(columns) or
                        any(not isinstance(c, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*', c) for c in columns) or
                        len(rows) > 10000 or any(set(row) != set(columns) for row in rows)):
                    raise ValueError('invalid or oversized table')
                table = path.stem
                self.db.execute(f'CREATE TABLE "{table}" (' + ','.join(f'"{c}" TEXT' for c in columns) + ')')
                self.db.executemany(f'INSERT INTO "{table}" VALUES (' + ','.join('?' for _ in columns) + ')',
                                    [[str(row[c]) if row[c] is not None else None for c in columns] for row in rows])
                self.tables[table] = columns
            self.db.commit()
            if hasattr(self.db, 'setlimit'):  # Available in Python >= 3.11.
                self.db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 32000)
            self.db.set_authorizer(self.authorize)
        except Exception:
            self.close()
            raise

    def close(self):
        self.db.close()

    @staticmethod
    def authorize(action, arg1, arg2, database, trigger):
        allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}
        functions = {'count','sum','avg','min','max','total','round','coalesce','ifnull','nullif',
                     'lower','upper','trim','ltrim','rtrim','length','substr','substring','replace',
                     'abs','date','strftime','typeof','instr','like','glob'}
        if action not in allowed or action == sqlite3.SQLITE_FUNCTION and str(arg2).lower() not in functions:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    def path(self, name):
        if not isinstance(name, str) or Path(name).is_absolute():
            raise ValueError('expected relative data path')
        path = (self.root / name).resolve(strict=True)
        if not path.is_relative_to(self.root) or not path.is_file():
            raise ValueError('path outside data folder or not a file')
        if path.stat().st_size > 2_000_000:
            raise ValueError('data file exceeds 2 MB')
        return path

    @staticmethod
    def page(args, default, maximum):
        offset, limit = args.get('offset', 0), args.get('limit', default)
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= maximum:
            raise ValueError('invalid pagination')
        return offset, limit

    def call(self, name, args):
        try:
            if not isinstance(args, dict):
                raise ValueError('args must be an object')
            if name == 'list_files':
                return {'files': [{'path': p.name, 'bytes': p.stat().st_size}
                                  for p in sorted(self.root.iterdir())
                                  if p.is_file() and not p.is_symlink()]}
            if name == 'schema':
                return {'tables': self.tables}
            if name == 'read_file':
                offset, limit = self.page(args, 4096, 8192)
                text = self.path(args.get('path')).read_text(encoding='utf-8')
                end = min(offset + limit, len(text))
                return {'text': text[offset:end], 'next_offset': end if end < len(text) else None}
            if name == 'read_csv':
                offset, limit = self.page(args, 20, 100)
                with self.path(args.get('path')).open(encoding='utf-8', newline='') as stream:
                    reader = csv.DictReader(stream)
                    columns, rows = reader.fieldnames, list(reader)
                end = min(offset + limit, len(rows))
                result = {'columns': columns, 'rows': rows[offset:end],
                          'next_offset': end if end < len(rows) else None}
                if len(json.dumps(result)) > 32000:
                    raise ValueError('CSV page exceeds 32 KB; request fewer rows or use SQL')
                return result
            if name == 'sql':
                query = args.get('query')
                if not isinstance(query, str) or not query.strip() or len(query) > 10000:
                    raise ValueError('invalid SQL query')
                # Bound VM work, including recursive CTEs and Cartesian products.
                remaining = [1000]
                def progress():
                    remaining[0] -= 1
                    return remaining[0] <= 0
                self.db.set_progress_handler(progress, 1000)
                try:
                    cursor = self.db.execute(query)
                    rows = cursor.fetchmany(101)
                    result = {'columns': [c[0] for c in cursor.description],
                              'rows': [list(row) for row in rows[:100]], 'truncated': len(rows) > 100}
                finally:
                    self.db.set_progress_handler(None, 0)
                if len(json.dumps(result)) > 32000:
                    raise ValueError('SQL result exceeds 32 KB; aggregate or shorten selected values')
                return result
            raise ValueError('unknown tool: ' + str(name))
        except (ValueError, OSError, sqlite3.Error, TypeError) as error:
            return {'error': str(error)[:500]}


class PaginatedToolbox(Toolbox):
    """A deliberately restricted CSV-only task environment; no SQL/file bypass."""
    help = {'list_files':TOOL_HELP['list_files'],
            'read_csv':'args {path, offset=0, limit=20}: read at most 20 CSV records; next_offset/null marks continuation',
            'aggregate':'args {path, operation: count|sum, field: column for sum, where: {column: exact_value}}: aggregate already observed records, requires all pages read'}

    def __init__(self, data_dir):
        self.root = Path(data_dir).resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError('data_dir must be a directory')
        self.observed = {}
        self.ends = {}

    def close(self):
        pass

    def call(self, name, args):
        if name not in self.help:
            return {'error':'Tool unavailable in this CSV-only task environment.'}
        if name == 'read_csv':
            try:
                if not isinstance(args, dict):
                    raise ValueError('args must be an object')
                self.page(args,20,20)
            except ValueError as error:
                return {'error':str(error)}
            result = super().call(name,args)
            if 'rows' in result:
                path = str(self.path(args.get('path')))
                offset = args.get('offset',0)
                observed = self.observed.setdefault(path,{})
                observed.update({offset+i:row for i,row in enumerate(result['rows'])})
                if result['next_offset'] is None:
                    self.ends[path] = offset+len(result['rows'])
            return result
        if name == 'aggregate':
            try:
                if not isinstance(args,dict) or args.get('operation') not in ('sum','count'):
                    raise ValueError('aggregate requires operation sum or count')
                path = str(self.path(args.get('path')))
                observed, end = self.observed.get(path,{}), self.ends.get(path)
                if end is None or len(observed)!=end or any(not 0<=i<end for i in observed):
                    raise ValueError('Read all pages before aggregate; some records have not been observed.')
                where = args.get('where',{})
                if not isinstance(where,dict) or len(where)>10:
                    raise ValueError('where must be an object with at most 10 columns')
                rows = list(observed.values())
                columns = set(rows[0]) if rows else set()
                if not set(where).issubset(columns):
                    raise ValueError('unknown filter column')
                selected = [row for row in rows if all(row[k]==str(v) for k,v in where.items())]
                value = len(selected)
                if args['operation']=='sum':
                    field = args.get('field')
                    if field not in columns:
                        raise ValueError('sum requires a known numeric field')
                    numbers = [Decimal(row[field]) for row in selected]
                    if not all(number.is_finite() for number in numbers):
                        raise ValueError('sum values must be finite')
                    value = str(sum(numbers,Decimal(0)))
                return {'value':value,'rows_observed':end,'rows_matched':len(selected),'complete':True}
            except (ValueError,TypeError,OSError,DecimalException) as error:
                return {'error':str(error)[:500]}
        return super().call(name,args)


def run_session(question, toolbox, choose, max_steps=10, max_context_chars=60000):
    """Each choose invocation is one action, then tools return observations."""
    history = []
    for step in range(max_steps):
        context = {'kind': 'datadesk', 'question': question, 'tools': getattr(toolbox,'help',TOOL_HELP), 'history': history}
        if len(json.dumps(context, ensure_ascii=False)) > max_context_chars:
            return {'answer': '', 'history': history, 'stop_reason': 'context_limit', 'steps': step}
        action = choose(context)
        if isinstance(action, dict) and set(action) == {'answer'} and type(action['answer']) in (str, int, float):
            return {'answer': str(action['answer']), 'history': history, 'stop_reason': 'answer', 'steps': step + 1}
        if not isinstance(action, dict) or set(action) != {'tool', 'args'} or not isinstance(action['tool'], str):
            result = {'error': 'Return exactly {"answer": value} or {"tool": name, "args": object}.'}
        else:
            result = toolbox.call(action['tool'], action['args'])
        history.append({'action': action, 'result': result})
        if len(history) >= 3 and history[-1] == history[-2] == history[-3]:
            return {'answer': '', 'history': history, 'stop_reason': 'repeated_action', 'steps': step + 1}
    return {'answer': '', 'history': history, 'stop_reason': 'step_limit', 'steps': max_steps}


def check_answer(tid, kind, answer, digest):
    text = str(answer).strip()
    if not text:
        return False
    if kind in ('int', 'number'):
        values = re.findall(r'-?\d+(?:\.\d+)?', text.replace(',', ''))
        if len(values) != 1:
            return False
        text = values[0]
    try:
        normalized = norm(kind, text)
        actual = hashlib.sha256(f'datadesk:{tid}:{normalized}'.encode()).hexdigest()
        return actual == digest
    except (ValueError, OverflowError):
        return False
