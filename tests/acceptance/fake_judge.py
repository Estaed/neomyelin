"""A stand-in model for acceptance tests: reads the stage's learnings.json, files every learning.

The engine runs the model CLI with the stage as its working folder, so this sees exactly what a
real model would. It never writes a file, like a well-behaved judge.
"""
import json
from pathlib import Path

items = json.loads(Path('learnings.json').read_text(encoding='utf-8'))['items']
print(json.dumps({
    'reviewed_ids': [item['id'] for item in items],
    'gaps': [{'id': item['id'], 'reason': 'No knowledge note covers this lesson yet.', 'target': 'new',
              'title': 'Otters hold hands asleep', 'note': 'Sea otters hold hands while sleeping.'}
             for item in items],
    'conflicts': [],
    'ideas': [],
}))
