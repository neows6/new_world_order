import json, glob, re

date_breakdown = {}
for f in sorted(glob.glob('data/daily_logs/2026-04-2*.jsonl')):
    date = f.replace('data/daily_logs/', '').replace('data\\daily_logs\\','').replace('.jsonl','').strip().split('/')[-1].split('\\')[-1].replace('.jsonl','')
    entries_by_key = {}
    for line in open(f, encoding='utf-8'):
        e = json.loads(line)
        key = (e.get('time', ''), e.get('ticker', ''))
        entries_by_key.setdefault(key, {})[e.get('model', '')] = e

    re_blocks = composite_blocks = would_pass = claude_buys = 0
    composites_at_buy = []

    for key, models in entries_by_key.items():
        if 'claude' in models and models['claude'].get('action') == 'BUY':
            claude_buys += 1
            s = models.get('standard', {})
            block = (s.get('blocking_reason') or '').lower()
            comp = s.get('composite_score')
            if comp is not None:
                composites_at_buy.append(comp)
            if 'turbulence' in block or 'reynolds' in block:
                re_blocks += 1
            elif 'composite' in block or 'hold' in block or 'fud' in block or 'signal' in block:
                composite_blocks += 1
            else:
                would_pass += 1

    if claude_buys:
        avg_c = sum(composites_at_buy)/len(composites_at_buy) if composites_at_buy else 0
        min_c = min(composites_at_buy) if composites_at_buy else 0
        print(f'{date}: claude_buys={claude_buys:3}  re_blocked={re_blocks:3}  composite_blocked={composite_blocks:3}  unblocked={would_pass:3}  avg_std_composite={avg_c:.4f}  min_std_composite={min_c:.4f}')

# What composite threshold would have unlocked Standard for composite-blocked trades?
print('\n--- Composite score distribution at composite-blocked moments ---')
scores = []
for f in sorted(glob.glob('data/daily_logs/2026-04-2*.jsonl')):
    entries_by_key = {}
    for line in open(f, encoding='utf-8'):
        e = json.loads(line)
        key = (e.get('time', ''), e.get('ticker', ''))
        entries_by_key.setdefault(key, {})[e.get('model', '')] = e
    for key, models in entries_by_key.items():
        if 'claude' in models and models['claude'].get('action') == 'BUY':
            s = models.get('standard', {})
            block = (s.get('blocking_reason') or '').lower()
            comp = s.get('composite_score')
            if comp is not None and ('composite' in block or 'hold' in block):
                scores.append(comp)

if scores:
    scores.sort()
    n = len(scores)
    p = lambda pct: scores[int(n * pct / 100)]
    print(f'  n={n}  min={min(scores):.4f}  p25={p(25):.4f}  p50={p(50):.4f}  p75={p(75):.4f}  p90={p(90):.4f}  max={max(scores):.4f}')
    # What threshold captures 50% / 75% / 90% of Claude BUYs?
    for pct in [50, 75, 90, 95]:
        threshold = p(100 - pct)
        captured = sum(1 for s in scores if s >= threshold)
        print(f'  Threshold {threshold:.4f} captures {captured}/{n} ({100*captured/n:.0f}%) of composite-blocked Claude BUYs')

# What are the Reynolds numbers on days with Re blocks?
print('\n--- Reynolds numbers on re-blocked days ---')
re_nums = []
for f in sorted(glob.glob('data/daily_logs/2026-04-2*.jsonl')):
    for line in open(f, encoding='utf-8'):
        e = json.loads(line)
        if e.get('model') == 'standard':
            block = e.get('blocking_reason') or ''
            m = re.search(r'Re=([0-9.]+)', block)
            if m:
                re_nums.append(float(m.group(1)))

if re_nums:
    re_nums.sort()
    n = len(re_nums)
    p = lambda pct: re_nums[int(n * pct / 100)]
    print(f'  n={n}  min={min(re_nums):.2f}  p25={p(25):.2f}  p50={p(50):.2f}  p75={p(75):.2f}  p90={p(90):.2f}  max={max(re_nums):.2f}')
