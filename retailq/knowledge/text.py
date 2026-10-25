import hashlib
import unicodedata

from retailq.knowledge.contracts import ChunkDraft


def normalize_text(text: str) -> str:
    return '\n'.join(line.rstrip() for line in unicodedata.normalize('NFC',text).replace('\r\n','\n').replace('\r','\n').split('\n')).strip()


def embedding_text(draft: ChunkDraft) -> str:
    return f'分类：{normalize_text(draft.category)}\n问题：{normalize_text(draft.questions)}\n答案：{normalize_text(draft.answer)}'


def content_fingerprint(draft: ChunkDraft) -> str:
    return hashlib.sha256(embedding_text(draft).encode('utf-8')).hexdigest()
