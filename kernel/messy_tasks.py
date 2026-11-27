"""Messy real-world DataDesk tasks: dirty strings, duplicate order IDs, TEST_ accounts, and policy.txt rules."""

from __future__ import annotations

import json
import random
from pathlib import Path

from .datadesk_tasks import CATEGORIES, CITIES, FIRST, LAST, PRODUCTS, TIERS, answer_hash, write_csv

MESSY_FAMILIES = (
    'clean_city_revenue',
    'dedup_status_count',
    'clean_category_profit',
    'net_tier_revenue',
    'top_clean_customer',
    'valid_repeat_customers',
)


def _messy_text(rng: random.Random, value: str) -> str:
    mode = rng.randrange(4)
    if mode == 0:
        return value
    if mode == 1:
        return value.upper()
    if mode == 2:
        return ' ' + value.lower() + ' '
    return value.title() + ' '


def _messy_money(rng: random.Random, amount: float) -> str:
    text = f'{amount:.2f}'
    return ('$' + text) if rng.random() < 0.45 else text


def _messy_shop(rng: random.Random):
    names = rng.sample([f'{f} {l}' for f in FIRST for l in LAST], rng.randint(10, 14))
    customers = [
        {'customer_id': f'C{i + 1:02d}', 'name': n,
         'city': rng.choice(CITIES), 'tier': rng.choice(TIERS)}
        for i, n in enumerate(names)
    ]
    # Add two internal TEST_ accounts that must be excluded per policy.txt.
    test_customers = [
        {'customer_id': 'TEST_01', 'name': 'QA Bot Alpha', 'city': CITIES[0], 'tier': 'gold'},
        {'customer_id': 'TEST_02', 'name': 'QA Bot Beta', 'city': CITIES[1], 'tier': 'silver'},
    ]
    prods = rng.sample(PRODUCTS, rng.randint(8, 10))
    cats = CATEGORIES[:]
    rng.shuffle(cats)
    products = [
        {'product': p, 'category': cats[i % len(cats)], 'unit_cost': round(rng.uniform(5, 50), 2)}
        for i, p in enumerate(prods)
    ]
    cost_map = {p['product']: p['unit_cost'] for p in products}
    clean_orders = []
    for i in range(rng.randint(70, 95)):
        p = rng.choice(prods)
        c = rng.choice(customers)
        clean_orders.append({
            'order_id': f'O{i + 1:04d}',
            'customer_id': c['customer_id'],
            'product': p,
            'qty': rng.randint(1, 8),
            'unit_price': round(cost_map[p] * rng.uniform(1.25, 1.85), 2),
            'status': rng.choice(['completed'] * 12 + ['cancelled'] * 4 + ['refunded'] * 4),
        })
    # Add high-value TEST_ orders so skipping policy.txt skews top_clean_customer and totals.
    test_orders = []
    for j in range(6):
        p = rng.choice(prods)
        test_orders.append({
            'order_id': f'OT{j + 1:03d}',
            'customer_id': test_customers[j % 2]['customer_id'],
            'product': p,
            'qty': 50,
            'unit_price': round(cost_map[p] * 2.0, 2),
            'status': 'completed',
        })

    discounts = {
        'gold': rng.choice([5, 10, 15]),
        'silver': rng.choice([2, 5, 8]),
        'bronze': 0,
    }

    # Build dirty serialized tables while preserving exact ground truth in clean_orders.
    raw_customers = [
        {'customer_id': c['customer_id'], 'name': c['name'],
         'city': _messy_text(rng, c['city']), 'tier': _messy_text(rng, c['tier'])}
        for c in customers + test_customers
    ]
    raw_products = [
        {'product': p['product'], 'category': _messy_text(rng, p['category']),
         'unit_cost': _messy_money(rng, p['unit_cost'])}
        for p in products
    ]
    raw_orders = []
    for o in clean_orders + test_orders:
        row = {
            'order_id': o['order_id'],
            'customer_id': o['customer_id'],
            'product': o['product'],
            'qty': str(o['qty']),
            'unit_price': _messy_money(rng, o['unit_price']),
            'status': _messy_text(rng, o['status']),
        }
        raw_orders.append(row)
        # Inject exact duplicate order_id rows (~15%) that must be deduplicated.
        if rng.random() < 0.15:
            raw_orders.append(dict(row))
    rng.shuffle(raw_orders)

    policy_text = (
        'Data Cleaning & Reporting Policy\n'
        '================================\n'
        '1. Exclude internal test accounts whose customer_id starts with "TEST_".\n'
        '2. orders.csv contains duplicate rows with identical order_id; deduplicate by order_id before counting or summing.\n'
        '3. Text columns (status, city, tier, category) have inconsistent case and leading/trailing spaces; trim and compare case-insensitively.\n'
        '4. Monetary columns (unit_price, unit_cost) may include a leading "$" symbol; strip "$" before casting to numeric.\n'
        f'5. Net tier revenue applies a customer-tier discount to completed orders: '
        f'gold={discounts["gold"]}%, silver={discounts["silver"]}%, bronze={discounts["bronze"]}% '
        '(net_revenue = qty * unit_price * (1 - discount_pct / 100)).\n'
    )
    return {
        'customers': customers,
        'products': products,
        'clean_orders': clean_orders,
        'discounts': discounts,
        'raw_customers': raw_customers,
        'raw_products': raw_products,
        'raw_orders': raw_orders,
        'policy_text': policy_text,
    }


def _messy_question(family: str, shop: dict, rng: random.Random):
    C, P, O = shop['customers'], shop['products'], shop['clean_orders']
    city_of = {c['customer_id']: c['city'] for c in C}
    tier_of = {c['customer_id']: c['tier'] for c in C}
    name_of = {c['customer_id']: c['name'] for c in C}
    cat_of = {p['product']: p['category'] for p in P}
    cost_of = {p['product']: p['unit_cost'] for p in P}
    done = [o for o in O if o['status'] == 'completed']

    if family == 'clean_city_revenue':
        city = rng.choice(sorted({c['city'] for c in C}))
        val = sum(o['qty'] * o['unit_price'] for o in done if city_of[o['customer_id']] == city)
        return (f'Following all reporting rules in the data directory, what is the total completed order revenue '
                f'(quantity x unit price) for customers in {city}? Round to 2 decimals.', 'number', val)
    if family == 'dedup_status_count':
        st = rng.choice(['completed', 'cancelled', 'refunded'])
        val = sum(1 for o in O if o['status'] == st)
        return (f'Following all reporting rules in the data directory, how many valid orders have status "{st}"?',
                'int', val)
    if family == 'clean_category_profit':
        cat = rng.choice(sorted({p['category'] for p in P}))
        val = sum(o['qty'] * (o['unit_price'] - cost_of[o['product']])
                  for o in done if cat_of[o['product']] == cat)
        return (f'Following all reporting rules in the data directory, what is the total profit '
                f'(quantity x (unit price - unit cost)) of completed orders in category "{cat}"? Round to 2 decimals.',
                'number', val)
    if family == 'net_tier_revenue':
        tier = rng.choice(['gold', 'silver'])
        disc = shop['discounts'][tier]
        val = sum(o['qty'] * o['unit_price'] * (1.0 - disc / 100.0)
                  for o in done if tier_of[o['customer_id']] == tier)
        return (f'Following all reporting rules in the data directory, what is the net completed revenue '
                f'(after the tier discount) for customers in the "{tier}" tier? Round to 2 decimals.',
                'number', val)
    if family == 'top_clean_customer':
        rev = {}
        for o in done:
            rev[o['customer_id']] = rev.get(o['customer_id'], 0.0) + o['qty'] * o['unit_price']
        top = sorted(rev.items(), key=lambda kv: -kv[1])
        if len(top) < 2 or abs(top[0][1] - top[1][1]) < 1.0:
            return None
        return ('Following all reporting rules in the data directory, which valid customer has the highest '
                'total completed revenue (quantity x unit price)? Give the customer name.', 'str', name_of[top[0][0]])
    if family == 'valid_repeat_customers':
        counts = {}
        for o in done:
            counts[o['customer_id']] = counts.get(o['customer_id'], 0) + 1
        val = sum(1 for c in counts.values() if c >= 2)
        return ('Following all reporting rules in the data directory, how many distinct valid customers '
                'have at least 2 completed orders?', 'int', val)
    raise ValueError(family)


def make_messy_tasks(root: Path, seed: int):
    rng = random.Random(seed)
    items = []
    for family in MESSY_FAMILIES:
        while True:
            shop = _messy_shop(rng)
            q = _messy_question(family, shop, rng)
            if q is not None:
                break
        tid = 'm-' + family
        directory = root / tid
        directory.mkdir(parents=True, exist_ok=True)
        write_csv(directory / 'customers.csv', shop['raw_customers'])
        write_csv(directory / 'orders.csv', shop['raw_orders'])
        (directory / 'products.json').write_text(json.dumps(shop['raw_products'], indent=1) + '\n', encoding='utf-8')
        (directory / 'policy.txt').write_text(shop['policy_text'], encoding='utf-8')
        question_text, kind, value = q
        items.append({
            'id': tid,
            'question': question_text,
            'type': kind,
            'digest': answer_hash(tid, kind, value),
            'directory': directory,
        })
    return items
