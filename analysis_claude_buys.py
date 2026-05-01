import json, glob, re
from collections import Counter

results = []
for f in sorted(glob.glob('data/daily_logs/2026-04-2*.jsonl')):
    entries_by_key = {}
    for line in open(f, encoding='utf-8'):
        e = json.loads(line)
        key = (e.get('time', ''), e.get('ticker', ''))
        mdl = e.get('model', '')
        if key not in entries_by_key:
            entries_by_key[key] = {}
        entries_by_key[key][mdl] = e
    for key, models in entries_by_key.items():
        if 'claude' in models and models['claude'].get('action') == 'BUY':
            s = models.get('standard', {})
            results.append({
                'std_block': s.get('blocking_reason', 'none'),
                'std_gates_failed': s.get('gates_failed', []),
                'std_reynolds': s.get('reynolds', 'N/A'),
                'std_composite': s.get('composite_score'),
                'std_ensemble': s.get('ensemble_prob'),
            })

print(f'Matched moments (Claude BUY vs Standard): {len(results)}')

blocks = Counter((r['std_block'] or 'none')[:90] for r in results)
print('\nWhat blocked Standard at Claude BUY times:')
for b, c in blocks.most_common(10):
    print(f'  x{c:3}  {b}')

re_nums = []
for r in results:
    block = r['std_block'] or ''
    m = re.search(r'Re=([0-9.]+)', block)
    if m:
        re_nums.append(float(m.group(1)))

if re_nums:
    re_nums.sort()
    n = len(re_nums)
    p = lambda pct: re_nums[int(n * pct / 100)]
    print(f'\nReynolds numbers in blocks (n={n}):')
    print(f'  min={min(re_nums):.2f}  p25={p(25):.2f}  p50={p(50):.2f}  p75={p(75):.2f}  p90={p(90):.2f}  max={max(re_nums):.2f}')

print('\nAll failed gates at Claude BUY moments (Standard):')
all_fails = Counter(g for r in results for g in r['std_gates_failed'])
for g, c in all_fails.most_common(10):
    print(f'  x{c:3}  {g[:90]}')

# Secondary blockers (if Reynolds were fixed, what else would still block?)
secondary = [r for r in results if r['std_gates_failed'] and not any('Reynolds' in g for g in r['std_gates_failed'])]
print(f'\nSecondary blocks (non-Reynolds gates failed): {len(secondary)}')
sec_fails = Counter(g for r in secondary for g in r['std_gates_failed'])
for g, c in sec_fails.most_common(10):
    print(f'  x{c:3}  {g[:90]}')
