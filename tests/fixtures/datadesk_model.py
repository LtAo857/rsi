"""Scripted offline adapter for integration tests, never a model benchmark.

Derives SQL from the question, then uses only the returned tool result.
"""
import json
import sys


def quote(value):
    return "'" + value.replace("'", "''") + "'"


def query_for(question):
    if question.startswith('In which city'):
        name = question.split('customer ', 1)[1].rsplit(' live?', 1)[0]
        return 'SELECT city FROM customers WHERE name=' + quote(name)
    if question.startswith('How many orders'):
        return 'SELECT COUNT(*) FROM orders WHERE status=' + quote(question.split("'")[1])
    if question.startswith('What is the total revenue'):
        month = question.split('placed in ')[1].split()[0]
        months = ['January','February','March','April','May','June']
        return ("SELECT ROUND(SUM(qty*unit_price),2) FROM orders WHERE status='completed' AND substr(date,6,2)="
                + quote(f'{months.index(month)+1:02d}'))
    if question.startswith('How many cancelled'):
        return ("SELECT COUNT(*) FROM orders o JOIN products p ON o.product=p.product WHERE o.status='cancelled' AND p.category="
                + quote(question.split("'")[1]))
    if question.startswith('Which customer'):
        return ("SELECT c.name FROM orders o JOIN customers c ON o.customer_id=c.customer_id WHERE o.status='completed' "
                "GROUP BY c.customer_id ORDER BY SUM(o.qty*o.unit_price) DESC LIMIT 1")
    if question.startswith('What is the average'):
        return ("SELECT ROUND(AVG(CAST(o.qty AS REAL)),2) FROM orders o JOIN customers c ON o.customer_id=c.customer_id "
                "WHERE o.status='completed' AND c.tier=" + quote(question.split("'")[1]))
    if question.startswith('What is the total profit'):
        return ("SELECT ROUND(SUM(o.qty*(o.unit_price-p.unit_cost)),2) FROM orders o JOIN products p ON o.product=p.product "
                "WHERE o.status='completed' AND p.category=" + quote(question.split("'")[1]))
    raise ValueError('unsupported fixture question')


def main():
    messages = json.load(sys.stdin)
    task = json.loads(messages['prompt'])
    history = task['history']
    if '--require-marker' in sys.argv and 'USE_SQL' not in messages['system']:
        action = {'answer':'incorrect'}
    elif not history:
        action = {'tool':'schema', 'args':{}}
    elif '--bad-query' in sys.argv and len(history) == 1:
        action = {'tool':'sql', 'args':{'query':'SELECT missing FROM orders'}}
    elif history[-1]['action'].get('tool') == 'sql' and 'rows' in history[-1]['result']:
        action = {'answer': history[-1]['result']['rows'][0][0]}
    else:
        action = {'tool':'sql', 'args':{'query':query_for(task['question'])}}
    print(json.dumps({'model_response':json.dumps(action)}))
    print(json.dumps({'type':'usage', 'input_tokens':1, 'output_tokens':1}))


if __name__ == '__main__':
    main()
