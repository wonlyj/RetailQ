from dataclasses import replace
import re

from retailq.errors import ServiceError
from retailq.knowledge.contracts import ConversationSnapshot
from retailq.knowledge.qa_gateway import extraction_cost
from retailq.knowledge.text import normalize_text


def batch_conversations(snapshots: list[ConversationSnapshot],*,max_conversations: int,input_budget: int) -> list[list[ConversationSnapshot]]:
    fragments=[]
    for snapshot in snapshots:
        if extraction_cost([snapshot])<=input_budget:
            fragments.append(snapshot);continue
        turns=[];current=[]
        for message in snapshot.messages:
            if message.role=='user' and current:
                turns.append(current);current=[]
            current.append(message)
        if current:turns.append(current)
        for i,turn in enumerate(turns):
            fragment=replace(snapshot,source_ref=snapshot.source_ref+f'#part{i+1}',messages=tuple(turn))
            if turn[0].role!='user' or turn[-1].role!='assistant' or extraction_cost([fragment])>input_budget:
                raise ServiceError('knowledge_conversation_too_large',422,f'来源 {snapshot.source_ref} 的完整轮次超预算，未截断。')
            fragments.append(fragment)
    batches=[];current=[]
    for fragment in fragments:
        if current and (len(current)>=max_conversations or extraction_cost(current+[fragment])>input_budget):
            batches.append(current);current=[]
        current.append(fragment)
    if current:batches.append(current)
    return batches


def validate_proposals(proposals,batch):
    sources={row.source_ref:row for row in batch}
    for proposal in proposals:
        source=sources.get(proposal.source_ref)
        if source is None:
            raise ServiceError('knowledge_qa_source_invalid',502,'知识抽取引用了未提供的来源。')
        users=[m.content or '' for m in source.messages if m.role=='user']
        answers=[normalize_text(m.content or '') for m in source.messages if m.role=='assistant' and m.content]
        # Whole answers/paragraphs preserve exceptions in adjacent sentences.
        evidence={unit for answer in answers for unit in [answer,*re.split(r'\n\s*\n',answer)] if unit}
        if not proposal.question.strip() or not any(proposal.question in text for text in users) or proposal.evidence not in evidence or proposal.answer!=proposal.evidence:
            raise ServiceError('knowledge_qa_evidence_invalid',502,'知识问法或完整答案缺少对应会话证据，未入暂存表。')


def extract_to_staging(repository,gateway,batch,batch_no):
    proposals=gateway.extract(batch)
    validate_proposals(proposals,batch)
    return repository.stage(batch_no,proposals)
