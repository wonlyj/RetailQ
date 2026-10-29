import json
from collections import Counter, defaultdict
from hashlib import sha256
from pathlib import Path
from retailq.knowledge.markdown import chunk_document, load_manifest
from retailq.knowledge.text import normalize_text
from retailq.knowledge.token_budget import BGETokenBudget

root = Path('evals')
cases = [json.loads(line) for line in (root/'ch04_cases.jsonl').read_text(encoding='utf-8').splitlines()]
checks = [json.loads(line) for line in (root/'ch04_prompt_checks.jsonl').read_text(encoding='utf-8').splitlines()]
docs = {d.key:d for d in load_manifest(Path('examples/knowledge/manifest.json'))}
budget = BGETokenBudget(Path('artifacts/knowledge/assets/tokenizer.json'))
chunks = {key: chunk_document(doc,budget) for key,doc in docs.items()}
family_splits=defaultdict(set)
for case in cases:
    family_splits[case['family_id']].add(case['split'])
    assert 1 <= len(case['query']) <= 512
    assert case['answerable'] == bool(case['relevant_groups'])
    assert case['required_facts'] and all(isinstance(f,str) and f for f in case['required_facts'])
    for group in case['relevant_groups']:
        assert group
        for ref in group:
            doc=docs[ref['source_key']]
            assert ref['source_version']==sha256(normalize_text(doc.text).encode()).hexdigest()
            matches=[c for c in chunks[doc.key] if ref['quote'] in c.answer and
                     c.section_path.split(' / ')[1] == ref['section_path']]
            assert matches,(case['eval_id'],ref)
            if case['filters'].get('category'):
                assert any(c.category==case['filters']['category'] for c in matches), case['eval_id']
assert len(cases)==100 and len(checks)>=20 and len({c['eval_id'] for c in cases})==100
assert all(len(s)==1 for s in family_splits.values())
assert Counter((c['bucket'],c['split']) for c in cases)=={(b,s):n for b in ('A_policy','B_model','C_colloquial','D_missing','E_multi') for s,n in (('calibration',4),('test',16))}
for row in checks:
    for ref in row['evidence']:
        doc=docs[ref['source_key']]
        assert ref['source_version']==sha256(normalize_text(doc.text).encode()).hexdigest()
        assert any(ref['quote'] in c.answer and c.section_path.split(' / ')[1]==ref['section_path'] for c in chunks[doc.key]), row['id']
print('VALIDATED cases=100 calibration=20 test=80 prompt_checks=%d families=%d chunks=%d' % (len(checks),len(family_splits),sum(map(len,chunks.values()))))
