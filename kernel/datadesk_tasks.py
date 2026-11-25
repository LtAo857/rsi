# Migrated from agent-rsi/examples/datadesk/build_tasks.py (local user project).
"""Generate the DataDesk task suite into a target repo.

Every task gets its own small synthetic shop (customers.csv, products.json,
orders.csv) from its own seed, and one question about it. Answers are stored
only as salted hashes (bench/answers.json), so neither the agent under test
nor the proposer can read them; this generator is NOT copied into the target.

Families (evolve uses the first five; held-out adds two it never saw):
    lookup          city of a named customer            (small file, easy)
    count_status    orders with a given status          (needs all of orders.csv)
    month_revenue   revenue of completed orders in a month
    category_cancel cancelled orders in a product category (join)
    top_customer    customer with the highest completed revenue (join + aggregate)
    tier_avg_qty    held-out only: mean quantity per completed order for a tier
    profit          held-out only: profit of a category (join with unit cost)
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import random
from pathlib import Path

FIRST = ["Ada", "Bruno", "Chen", "Dara", "Elif", "Farah", "Goran", "Hana", "Ivo", "Jun",
         "Kemal", "Lena", "Mateo", "Nia", "Omar", "Priya", "Quinn", "Rosa", "Sven", "Tariq",
         "Uma", "Viktor", "Wen", "Yara", "Zane"]
LAST = ["Abara", "Brandt", "Costa", "Dimitrov", "Eklund", "Fischer", "Garcia", "Haddad",
        "Ito", "Jensen", "Kovac", "Lindqvist", "Moreau", "Nakamura", "Okafor", "Petrov",
        "Quist", "Rahman", "Silva", "Tanaka", "Ueda", "Varga", "Weber", "Yilmaz", "Zhou"]
CITIES = ["Lisbon", "Osaka", "Tallinn", "Porto", "Lyon", "Gdansk", "Malmo", "Leeds"]
TIERS = ["gold", "silver", "bronze"]
CATEGORIES = ["kitchen", "garden", "office", "outdoor", "lighting"]
PRODUCTS = ["kettle", "trowel", "stapler", "lantern", "desk lamp", "planter", "notebook",
            "camp stove", "toaster", "hose", "monitor arm", "string lights", "whisk",
            "pruner", "tent peg set"]
MONTHS = ["January", "February", "March", "April", "May", "June"]
STATUSES = ["completed"] * 14 + ["cancelled"] * 3 + ["refunded"] * 3

EVOLVE = ["lookup", "count_status", "month_revenue", "category_cancel", "top_customer"]
HELDOUT_ONLY = ["tier_avg_qty", "profit"]
HARD_EXTRA = ["net_city_revenue", "top_profit_product", "repeat_customer_count"]


def norm(kind: str, value) -> str:
    """Must match bench/grade.py: the hash is taken over this form."""
    if kind == "int":
        return str(int(round(float(value))))
    if kind == "number":
        return f"{float(value):.2f}"
    return " ".join(str(value).casefold().split()).rstrip(".")


def answer_hash(tid: str, kind: str, value) -> str:
    return hashlib.sha256(f"datadesk:{tid}:{norm(kind, value)}".encode()).hexdigest()


def shop(rng: random.Random) -> dict:
    names = rng.sample([f"{f} {l}" for f in FIRST for l in LAST], rng.randint(12, 18))
    customers = [{"customer_id": f"C{i + 1:02d}", "name": n, "city": rng.choice(CITIES),
                  "tier": rng.choice(TIERS)} for i, n in enumerate(names)]
    prods = rng.sample(PRODUCTS, rng.randint(8, 11))
    cats = CATEGORIES[:]
    rng.shuffle(cats)
    products = [{"product": p, "category": cats[i % len(cats)],
                 "unit_cost": round(rng.uniform(3, 60), 2)} for i, p in enumerate(prods)]
    cost = {p["product"]: p["unit_cost"] for p in products}
    orders = []
    for i in range(rng.randint(90, 140)):
        p = rng.choice(prods)
        m = rng.randint(1, 6)
        orders.append({"order_id": f"O{i + 1:04d}", "date": f"2025-{m:02d}-{rng.randint(1, 28):02d}",
                       "customer_id": rng.choice(customers)["customer_id"], "product": p,
                       "qty": rng.randint(1, 9),
                       "unit_price": round(cost[p] * rng.uniform(1.2, 1.9), 2),
                       "status": rng.choice(STATUSES)})
    orders.sort(key=lambda o: o["date"])
    return {"customers": customers, "products": products, "orders": orders}


def question(family: str, s: dict, rng: random.Random):
    """(question, kind, answer) or None when this shop has no unambiguous answer."""
    C, P, O = s["customers"], s["products"], s["orders"]
    cat = {p["product"]: p["category"] for p in P}
    cost = {p["product"]: p["unit_cost"] for p in P}
    name = {c["customer_id"]: c["name"] for c in C}
    tier = {c["customer_id"]: c["tier"] for c in C}
    done = [o for o in O if o["status"] == "completed"]
    if family == "lookup":
        c = rng.choice(C)
        return f"In which city does the customer {c['name']} live?", "str", c["city"]
    if family == "count_status":
        st = rng.choice(["cancelled", "refunded", "completed"])
        return (f"How many orders have the status '{st}'?", "int",
                sum(o["status"] == st for o in O))
    if family == "month_revenue":
        m = rng.randint(1, 6)
        v = sum(o["qty"] * o["unit_price"] for o in done if int(o["date"][5:7]) == m)
        return (f"What is the total revenue (quantity x unit price) of completed orders "
                f"placed in {MONTHS[m - 1]} 2025? Round to 2 decimals.", "number", v)
    if family == "category_cancel":
        k = rng.choice(sorted({p["category"] for p in P}))
        return (f"How many cancelled orders were for products in the '{k}' category?", "int",
                sum(o["status"] == "cancelled" and cat[o["product"]] == k for o in O))
    if family == "top_customer":
        rev: dict = {}
        for o in done:
            rev[o["customer_id"]] = rev.get(o["customer_id"], 0) + o["qty"] * o["unit_price"]
        top = sorted(rev.items(), key=lambda kv: -kv[1])
        if len(top) > 1 and abs(top[0][1] - top[1][1]) < 1:
            return None
        return ("Which customer has the highest total revenue (quantity x unit price) from "
                "completed orders? Give the customer's name.", "str", name[top[0][0]])
    if family == "tier_avg_qty":
        t = rng.choice(TIERS)
        q = [o["qty"] for o in done if tier[o["customer_id"]] == t]
        if not q:
            return None
        return (f"What is the average quantity per completed order for customers in the "
                f"'{t}' tier? Round to 2 decimals.", "number", sum(q) / len(q))
    if family == "profit":
        k = rng.choice(sorted({p["category"] for p in P}))
        v = sum(o["qty"] * (o["unit_price"] - cost[o["product"]]) for o in done
                if cat[o["product"]] == k)
        return (f"What is the total profit, quantity x (unit price - unit cost), of completed "
                f"orders for products in the '{k}' category? Round to 2 decimals.", "number", v)
    if family == "net_city_revenue":
        city_map = {c["customer_id"]: c["city"] for c in C}
        city = rng.choice(sorted({c["city"] for c in C}))
        v = sum(o["qty"] * o["unit_price"] for o in done if city_map[o["customer_id"]] == city)
        return (f"What is the total revenue (quantity x unit price) of completed orders "
                f"from customers living in {city}? Round to 2 decimals.", "number", v)
    if family == "top_profit_product":
        prof: dict = {}
        for o in done:
            prof[o["product"]] = prof.get(o["product"], 0.0) + o["qty"] * (o["unit_price"] - cost[o["product"]])
        top = sorted(prof.items(), key=lambda kv: -kv[1])
        if len(top) < 2 or abs(top[0][1] - top[1][1]) < 1:
            return None
        return ("Which product generated the highest total profit, quantity x (unit price - unit cost), "
                "across completed orders? Give the product name.", "str", top[0][0])
    if family == "repeat_customer_count":
        counts: dict = {}
        for o in done:
            counts[o["customer_id"]] = counts.get(o["customer_id"], 0) + 1
        repeaters = sum(1 for c in counts.values() if c >= 2)
        return ("How many distinct customers have at least 2 completed orders?", "int", repeaters)
    raise ValueError(family)


def write_csv(path: Path, rows: list[dict]) -> None:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows[0]), lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    path.write_text(buf.getvalue(), encoding="utf-8")


def make_split(root: Path, split: str, prefix: str, plan: list[tuple[str, int]],
               seed: int, answers: dict, plain: dict | None = None) -> list[str]:
    meta, ids = {}, []
    rng = random.Random(seed)
    for family, n in plan:
        made = 0
        while made < n:
            s = shop(rng)
            q = question(family, s, rng)
            if q is None:
                continue
            tid = f"{prefix}-{family.replace('_', '')}-{made}"
            d = root / "tasks" / split / tid
            d.mkdir(parents=True, exist_ok=True)
            write_csv(d / "customers.csv", s["customers"])
            write_csv(d / "orders.csv", s["orders"])
            (d / "products.json").write_text(json.dumps(s["products"], indent=1) + "\n")
            text, kind, value = q
            meta[tid] = {"question": text, "type": kind}
            answers[tid] = answer_hash(tid, kind, value)
            if plain is not None:
                plain[tid] = (kind, value)
            ids.append(tid)
            made += 1
    (root / "tasks" / f"{split}.json").write_text(json.dumps(meta, indent=1) + "\n")
    (root / "tasks" / f"{split}.txt").write_text("\n".join(ids) + "\n")
    return ids


def generate(root: Path, n_evolve: int = 3, n_heldout: int = 2, n_new: int = 5,
             plain: dict | None = None) -> dict:
    """Write the suite; `plain` (tests only) receives {task_id: (kind, answer)}."""
    root = Path(root)
    answers: dict = {}
    out = {
        "evolve": make_split(root, "evolve", "e", [(f, n_evolve) for f in EVOLVE], 101,
                             answers, plain),
        "heldout": make_split(root, "heldout", "h", [(f, n_heldout) for f in EVOLVE] +
                              [(f, n_new) for f in HELDOUT_ONLY], 202, answers, plain),
        "smoke": make_split(root, "smoke", "s", [("lookup", 1)], 303, answers, plain),
    }
    (root / "bench").mkdir(exist_ok=True)
    (root / "bench" / "answers.json").write_text(json.dumps(answers, indent=1) + "\n")
    return out


if __name__ == "__main__":
    import sys
    print(json.dumps({k: len(v) for k, v in generate(Path(sys.argv[1])).items()}))

