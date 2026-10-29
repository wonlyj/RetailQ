"""Load calibration lazily, keeping management available before calibration."""
from hashlib import sha256
from pathlib import Path

from retailq.knowledge.answer import ConfidencePolicy


def policy_identity(settings, snapshot, strategy):
    project = Path(__file__).resolve().parents[2]
    prompt = (project / 'retailq/prompts/rag_answer.txt').read_bytes()
    query = (project / 'retailq/prompts/query_normalize.txt').read_bytes()
    synonyms = (project / 'examples/knowledge/synonyms.json').read_bytes()
    model_identity = '|'.join((settings.embedding_model, settings.rerank_model if strategy == 'hybrid_rerank' else '-',
                               settings.openai_model, settings.embedding_base_url.rstrip('/'), settings.rerank_base_url.rstrip('/'),
                               settings.openai_base_url.rstrip('/')))
    return {'model':model_identity,
            'corpus_hash':snapshot.corpus_hash, 'prompt_hash':sha256(prompt).hexdigest(),
            'query_hash':sha256(query + b'\0' + synonyms).hexdigest(), 'strategy':strategy}


class DynamicConfidencePolicy:
    def __init__(self, settings): self.settings=settings

    def allows(self, result):
        identity=policy_identity(self.settings,result.snapshot,result.strategy)
        path=Path(self.settings.rag_confidence_path)
        if not path.is_absolute(): path=Path(__file__).resolve().parents[2]/path
        return ConfidencePolicy.load(path,expected_identity=identity).allows(result)
