"""Offline paging fixture; not evidence of model learning or prompt efficacy."""
import json
import sys

messages = json.load(sys.stdin)
task = json.loads(messages['prompt'])
pages = [row['result'] for row in task['history']
         if isinstance(row['result'].get('rows'),list)]
incomplete = bool(pages) and pages[-1]['next_offset'] is not None
stop_early = '--first-page' in sys.argv or ('--respect-hint' in sys.argv and
                                          'Read all pages needed' not in messages['system'])
if not pages or incomplete and not stop_early:
    action = {'tool':'read_csv','args':{'path':'records.csv','offset':pages[-1]['next_offset'] if pages else 0}}
else:
    rows = [row for page in pages for row in page['rows'] if row['status']=='active']
    answer = sum(int(row['qty']) for row in rows) if task['question'].startswith('Sum') else len(rows)
    action = {'answer':answer}
print(json.dumps({'model_response':json.dumps(action)}))
print(json.dumps({'type':'usage','input_tokens':1,'output_tokens':1}))
