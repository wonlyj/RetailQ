"""Legacy JSON helpers; conversation publication now requires review."""
import json
import os
from pathlib import Path

from retailq.knowledge.contracts import ConversationSnapshot, MiningMessage


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, path)


def snapshot_from_json(data):
    return ConversationSnapshot(data['source_ref'], data['conversation_id'], data['max_message_id'],
                                tuple(MiningMessage(**m) for m in data['messages']))
