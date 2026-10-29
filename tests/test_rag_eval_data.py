from pathlib import Path

import pytest

from retailq.knowledge.evaluation.contracts import read_cases


def test_cases_validate_and_keep_family_splits_separate():
    cases = read_cases(Path('evals/ch04_cases.jsonl'))
    assert len(cases) == 100
    assert sum(c.split == 'calibration' for c in cases) == 20
    assert sum(c.split == 'test' for c in cases) == 80
    assert all(not c.relevant_groups for c in cases if not c.answerable)


def test_bad_cases_fail_closed(tmp_path):
    (tmp_path/'cases.jsonl').write_text('{"eval_id":"A01"}\n',encoding='utf-8')
    with pytest.raises(ValueError):
        read_cases(tmp_path/'cases.jsonl')


def test_ground_truth_maps_to_frozen_source_chunks():
    from hashlib import sha256
    from types import SimpleNamespace
    from retailq.knowledge.evaluation.contracts import load_cases
    from retailq.knowledge.markdown import load_manifest, chunk_document
    from retailq.knowledge.text import normalize_text, content_fingerprint
    from retailq.knowledge.token_budget import BGETokenBudget

    budget=BGETokenBudget(Path('artifacts/knowledge/assets/tokenizer.json'))
    sources=[]; records={}; next_id=1
    for doc in load_manifest(Path('examples/knowledge/manifest.json')):
        ids=[]
        for draft in chunk_document(doc,budget):
            records[next_id]=SimpleNamespace(id=next_id,draft=draft,vector_id=str(next_id),vectorize_status='done')
            ids.append(next_id);next_id+=1
        sources.append(SimpleNamespace(key=doc.key,digest=sha256(normalize_text(doc.text).encode()).hexdigest(),chunk_ids=tuple(ids)))
    snapshot=SimpleNamespace(sources=tuple(sources),active_ids=tuple(records),retired_ids=(),approved_ids=(),
                             fingerprints=tuple((i,content_fingerprint(r.draft)) for i,r in records.items()))
    repository=SimpleNamespace(get_chunks=lambda ids: {i:records[i] for i in ids if i in records})
    cases=load_cases(Path('evals/ch04_cases.jsonl'), snapshot, repository=repository)
    assert all(case.groups for case in cases if case.case.answerable)
    sources[1].digest='0'*64
    with pytest.raises(ValueError):
        load_cases(Path('evals/ch04_cases.jsonl'), snapshot, repository=repository)
