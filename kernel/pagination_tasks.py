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
