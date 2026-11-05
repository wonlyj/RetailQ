"""Load calibration lazily, keeping management available before calibration."""
from hashlib import sha256
import json
from pathlib import Path

from retailq.knowledge.answer import ConfidencePolicy


def policy_identity(settings, snapshot, strategy, *, version=1):
    if version not in (1, 2): raise ValueError('unknown policy identity version')
    project = Path(__file__).resolve().parents[2]
    prompt = (project / 'retailq/prompts/rag_answer.txt').read_bytes()
    judge_prompt = (project / 'retailq/knowledge/evaluation/judge.py').read_bytes()
    query = (project / 'retailq/prompts/query_normalize.txt').read_bytes()
    query_code = (project / 'retailq/knowledge/query.py').read_bytes()
    synonyms = (project / 'examples/knowledge/synonyms.json').read_bytes()
    model_identity = '|'.join((settings.embedding_model, settings.rerank_model,
                               settings.openai_model, settings.embedding_base_url.rstrip('/'), settings.rerank_base_url.rstrip('/'),
                               settings.openai_base_url.rstrip('/'), settings.structured_output_method,
                               str(settings.input_budget), str(settings.max_output_tokens)))
    if version == 2:
        query_code += b'\0' + (project / 'retailq/knowledge/retrieval.py').read_bytes()
        query_code += b'\0' + (project / 'retailq/knowledge/rerank.py').read_bytes()
        query_code += b'\0' + (project / 'retailq/knowledge/milvus_store.py').read_bytes()
        query_code += b'\0' + (project / 'retailq/knowledge/embedding.py').read_bytes()
        query_code += b'\0' + (project / 'retailq/knowledge/text.py').read_bytes()
        query_code += b'\0' + (project / 'scripts/calibrate_rag.py').read_bytes()
    return {'model':model_identity,
            'corpus_hash':snapshot.corpus_hash, 'prompt_hash':sha256(prompt + b'\0' + judge_prompt).hexdigest(),
            'query_hash':sha256(query + b'\0' + query_code + b'\0' + synonyms).hexdigest(), 'strategy':'all' if version == 1 else 'all-v2'}


def _policy_from_bytes(raw, settings, snapshot):
    from retailq.errors import ServiceError
    try:
        strategy = json.loads(raw.decode('utf-8'))['identity']['strategy']
        if strategy not in ('all', 'all-v2'): raise ValueError('unknown policy version')
        expected = policy_identity(settings, snapshot, 'all', version=2 if strategy == 'all-v2' else 1)
    except (UnicodeDecodeError, ValueError, TypeError, KeyError, AttributeError):
        raise ServiceError('rag_policy_unavailable',503,'知识置信度配置缺失或与当前版本不一致，请先完成校准。') from None
    return ConfidencePolicy.from_bytes(raw, expected_identity=expected)


class DynamicConfidencePolicy:
    def __init__(self, settings): self.settings=settings

    def allows(self, result):
        path=Path(self.settings.rag_confidence_path)
        if not path.is_absolute(): path=Path(__file__).resolve().parents[2]/path
        try: raw=path.read_bytes()
        except OSError:
            from retailq.errors import ServiceError
            raise ServiceError('rag_policy_unavailable',503,'知识置信度配置缺失或与当前版本不一致，请先完成校准。') from None
        return _policy_from_bytes(raw,self.settings,result.snapshot).allows(result)


def freeze_evaluation_policy(settings, snapshot):
    path=Path(settings.rag_confidence_path)
    if not path.is_absolute(): path=Path(__file__).resolve().parents[2]/path
    try:
        raw=path.read_bytes()
    except OSError:
        from retailq.errors import ServiceError
        raise ServiceError('rag_policy_unavailable',503,'知识置信度配置缺失或与当前版本不一致，请先完成校准。') from None
    return _policy_from_bytes(raw,settings,snapshot),sha256(raw).hexdigest()
