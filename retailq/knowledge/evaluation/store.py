"""Atomic local checkpoints; an existing run can never be silently replaced."""
import json
from pathlib import Path
from retailq.knowledge.corpus import _write


class EvaluationStore:
    def __init__(self, root):
        self.root = Path(root)

    @classmethod
    def open_new(cls, output, identity):
        root=Path(output)
        root.mkdir(parents=True,exist_ok=False)
        _write(root/'manifest.json', {'identity':identity,'status':'running'})
        _write(root/'checkpoint.json', {'rows':[]})
        return cls(root)

    @classmethod
    def resume(cls, output, expected_identity):
        instance=cls(output)
        manifest=instance.manifest()
        if manifest['status'] not in ('running','finalizing') or manifest['identity'] != expected_identity:
            raise ValueError('evaluation identity or status mismatch')
        instance.rows()
        return instance

    def manifest(self):
        return json.loads((self.root/'manifest.json').read_text(encoding='utf-8'))

    def rows(self):
        data=json.loads((self.root/'checkpoint.json').read_text(encoding='utf-8'))
        if set(data)!={'rows'} or not isinstance(data['rows'],list):
            raise ValueError('invalid evaluation checkpoint')
        keys=[(row['eval_id'],row['strategy']) for row in data['rows']]
        if len(keys)!=len(set(keys)):
            raise ValueError('duplicate strategy checkpoint')
        return data['rows']

    def understanding(self, eval_id):
        path=self.root/'understanding.json'
        data=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        return data.get(eval_id)

    def save_understanding(self, eval_id, understanding):
        if self.manifest()['status'] != 'running': raise ValueError('evaluation is not writable')
        path=self.root/'understanding.json'
        data=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        if eval_id in data and data[eval_id] != understanding: raise ValueError('understanding cannot change')
        data[eval_id]=understanding
        _write(path,data)

    def errors(self):
        path=self.root/'errors.json'
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else []

    def record_error(self, eval_id, strategy, phase, code):
        if self.manifest()['status'] != 'running': raise ValueError('evaluation is not writable')
        errors=self.errors()
        errors.append({'eval_id':eval_id,'strategy':strategy,'phase':phase,'code':code})
        _write(self.root/'errors.json',errors)

    def checkpoint(self, row):
        if self.manifest()['status'] != 'running':
            raise ValueError('evaluation is not writable')
        phases=('understanding','retrieval','generation','judging','ledger','complete')
        if row.get('phase') not in phases:
            raise ValueError('invalid checkpoint phase')
        rows=self.rows()
        index=next((i for i,r in enumerate(rows) if (r['eval_id'],r['strategy']) == (row['eval_id'],row['strategy'])),None)
        if index is None:
            rows.append(row)
        else:
            current=rows[index]
            if phases.index(row['phase']) <= phases.index(current['phase']):
                raise ValueError('duplicate or regressive checkpoint')
            for key in row.keys() & current.keys() - {'phase'}:
                if row[key] != current[key]:
                    raise ValueError('checkpoint evidence cannot be changed')
            rows[index]={**current,**row}
        _write(self.root/'checkpoint.json', {'rows':rows})

    def begin_sealing(self, reason):
        if not isinstance(reason,str) or not reason.strip() or self.manifest()['status']!='running':
            raise ValueError('invalid seal')
        manifest=self.manifest();manifest.update(status='sealing',reason=reason)
        _write(self.root/'manifest.json',manifest)

    def finish_sealing(self):
        manifest=self.manifest()
        if manifest['status']!='sealing': raise ValueError('evaluation not sealing')
        manifest['status']='sealed'
        _write(self.root/'manifest.json',manifest)

    def seal(self, reason):
        self.begin_sealing(reason)
        self.finish_sealing()

    def begin_finalization(self):
        manifest=self.manifest()
        rows=self.rows()
        strategies={'dense','bm25','hybrid','hybrid_rerank'}
        ids={row['eval_id'] for row in rows}
        expected_ids=manifest['identity'].get('case_ids')
        if (manifest['status']!='running' or len(rows)!=manifest['identity']['expected']
                or any(row['phase']!='complete' for row in rows)
                or {(r['eval_id'],r['strategy']) for r in rows}!={(i,s) for i in ids for s in strategies}
                or (expected_ids is not None and ids!=set(expected_ids))):
            raise ValueError('evaluation receipts incomplete')
        manifest['status']='finalizing'
        _write(self.root/'manifest.json',manifest)

    def finish_finalization(self):
        manifest=self.manifest()
        if manifest['status']!='finalizing': raise ValueError('evaluation not finalizing')
        manifest['status']='completed'
        _write(self.root/'manifest.json',manifest)

    def complete(self):
        self.begin_finalization()
        self.finish_finalization()
