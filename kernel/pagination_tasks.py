"""Fixed tasks for a paginated CSV-only interface, not unrestricted SQL tasks."""

import csv
import random

from .datadesk_tasks import answer_hash


def make_pagination_tasks(root, seed):
    rng = random.Random(seed)
    items = []
    for family in ('active_sum', 'active_count'):
        tid = 'p-' + family
        directory = root / tid
        directory.mkdir()
        rows = [{'id':str(i), 'status':rng.choice(['active','inactive']), 'qty':rng.randint(1,25)}
                for i in range(rng.randint(61,79))]
        # Later pages always contribute; reading only the first page cannot suffice.
        rows[-1]['status'] = 'active'
        with (directory / 'records.csv').open('w', newline='', encoding='utf-8') as stream:
            writer = csv.DictWriter(stream, fieldnames=['id','status','qty'])
            writer.writeheader()
            writer.writerows(rows)
        value = (sum(row['qty'] for row in rows if row['status']=='active') if family=='active_sum'
                 else sum(row['status']=='active' for row in rows))
        question = ('Sum qty across all records with status active.' if family=='active_sum' else
                    'Count all records with status active.')
        items.append({'id':tid, 'question':question + ' Data is in records.csv, available through read_csv.',
                      'type':'int', 'digest':answer_hash(tid,'int',value), 'directory':directory})
    return items


REGIONS = ('north', 'south', 'east', 'west')
PRIORITIES = ('high', 'medium', 'low')


def make_extended_pagination_tasks(root, seed):
    """Six paginated CSV-only tasks with single and multi-column filters (4-5 pages)."""
    rng = random.Random(seed)
    region = rng.choice(REGIONS)
    priority = rng.choice(PRIORITIES)
    specs = (
        ('active_sum', 'sum', {'status': 'active'},
         'Sum qty across all records with status active.'),
        ('active_count', 'count', {'status': 'active'},
         'Count all records with status active.'),
        ('region_active_sum', 'sum', {'status': 'active', 'region': region},
         f"Sum qty across all records with status active and region {region}."),
        ('region_active_count', 'count', {'status': 'active', 'region': region},
         f"Count all records with status active and region {region}."),
        ('priority_active_sum', 'sum', {'status': 'active', 'priority': priority},
         f"Sum qty across all records with status active and priority {priority}."),
        ('priority_active_count', 'count', {'status': 'active', 'priority': priority},
         f"Count all records with status active and priority {priority}."),
    )
    items = []
    for family, op, criteria, prompt in specs:
        tid = 'px-' + family
        directory = root / tid
        directory.mkdir()
        rows = [{'id': str(i),
                 'status': rng.choice(['active', 'inactive']),
                 'region': rng.choice(REGIONS),
                 'priority': rng.choice(PRIORITIES),
                 'qty': rng.randint(1, 25)}
                for i in range(rng.randint(65, 95))]
        # Guarantee the final page always contributes to the target aggregate.
        for col, val in criteria.items():
            rows[-1][col] = val
        with (directory / 'records.csv').open('w', newline='', encoding='utf-8') as stream:
            writer = csv.DictWriter(stream, fieldnames=['id', 'status', 'region', 'priority', 'qty'])
            writer.writeheader()
            writer.writerows(rows)
        matched = [row for row in rows if all(row[k] == v for k, v in criteria.items())]
        value = sum(row['qty'] for row in matched) if op == 'sum' else len(matched)
        items.append({'id': tid,
                      'question': prompt + ' Data is in records.csv, available through read_csv.',
                      'type': 'int', 'digest': answer_hash(tid, 'int', value), 'directory': directory})
    return items
