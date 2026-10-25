import json
from pathlib import Path

import pytest

from retailq.knowledge.contracts import ChunkDraft
from retailq.knowledge.text import embedding_text


class Budget:
    def __init__(self,limit=8192): self.limit=limit
    def count(self,text): return len(text)
    def validate(self,text):
        if self.count(text)>self.limit: raise ValueError('token budget exceeded')


def document(text,kind='policy'):
    from retailq.knowledge.contracts import SourceDocument
    return SourceDocument('demo.md',text,kind,'演示根分类',('售后/退货',))


def chunk(text,**kwargs):
    from retailq.knowledge.markdown import chunk_document
    return chunk_document(document(text),Budget(),**kwargs)


def test_policy_heading_path_and_faq_real_questions():
    from retailq.knowledge.markdown import chunk_document
    rows=chunk('# 售后\n## 退货\n演示：七天内申请。\n')
    assert rows[0].category=='售后' and rows[0].questions=='退货'
    assert rows[0].is_key_clause
    assert 'demo.md@' in rows[0].section_path and '售后/退货' in rows[0].section_path
    faq=chunk_document(document('# 商品FAQ\n## 杯子能放洗碗机吗？\n问法：这个杯子可以机洗吗？\n演示：请手洗。','faq'),Budget())
    assert faq[0].questions=='杯子能放洗碗机吗？\n这个杯子可以机洗吗？'
    assert faq[0].answer=='演示：请手洗。'


def test_fenced_code_is_not_heading():
    rows=chunk('# 手册\n```text\n# 不是章节\n```\n操作完成。')
    assert len(rows)==1
    assert rows[0].questions=='手册' and '# 不是章节' in rows[0].answer


def test_complete_sentence_overlap_never_loses_new_text():
    sentences=[f'第{i}条：'+('完整知识' * 4)+'。' for i in range(12)]
    rows=chunk('# 规则\n'+''.join(sentences),target_chars=90,overlap_chars=30)
    assert len(rows)>2
    assert all(r.answer.endswith('。') for r in rows)
    assert all(any(sentence in r.answer for r in rows) for sentence in sentences)
    assert all(set(a.answer.split('。')) & set(b.answer.split('。')) - {''} for a,b in zip(rows,rows[1:]))
    assert len({r.section_path for r in rows})==len(rows)


def test_table_chunks_repeat_header_with_escaped_pipes():
    header='| 商品 | 说明 |\n| --- | --- |'
    body=[f'| 商品{i} | 内容\\|保留{i} |' for i in range(15)]
    rows=chunk('# 商品表\n'+header+'\n'+'\n'.join(body),target_chars=100,overlap_chars=20)
    assert len(rows)>2
    assert all(r.answer.startswith(header) for r in rows)
    assert all(any(line in r.answer for r in rows) for line in body)
    assert all(len(r.answer.splitlines())>=3 for r in rows)


def test_oversized_sentence_or_row_reports_source():
    from retailq.knowledge.markdown import chunk_document
    for text in ['# 大句\n'+'知识'*200+'。','# 表\n|标题|\n|---|\n|'+'内容'*200+'|']:
        with pytest.raises(ValueError,match='demo.md'):
            chunk_document(document(text),Budget(100))


def test_heading_and_header_count_towards_token_limit():
    from retailq.knowledge.markdown import chunk_document
    with pytest.raises(ValueError,match='demo.md'):
        chunk_document(document('# '+'标题'*40+'\n答案。'),Budget(90))
    with pytest.raises(ValueError,match='demo.md'):
        chunk_document(document('# 表\n|'+'表头'*50+'|\n|---|\n|短行|'),Budget(100))


def test_metadata_is_absent_from_embedding_text():
    draft=ChunkDraft('配送','运费标准','演示：满99元免运费。','META_ONLY','policy',True)
    assert embedding_text(draft)=='分类：配送\n问题：运费标准\n答案：演示：满99元免运费。'
    assert 'META_ONLY' not in embedding_text(draft)


def test_manifest_path_escape_and_field_overflow(tmp_path):
    from retailq.knowledge.markdown import load_manifest
    source=tmp_path/'manifest.json'
    source.write_text(json.dumps({'files':[{'path':'../outside.md','content_type':'policy','root_category':'根'}]}),encoding='utf-8')
    with pytest.raises(ValueError): load_manifest(source)
    with pytest.raises(ValueError): chunk('# '+'超长'*300+'\n知识。')


def test_real_tokenizer_counts_without_padding_or_truncation(tmp_path):
    from tokenizers import Tokenizer,models,pre_tokenizers
    from retailq.knowledge.token_budget import BGETokenBudget
    tokenizer=Tokenizer(models.WordLevel({'[UNK]':0,'hello':1}))
    tokenizer.pre_tokenizer=pre_tokenizers.Whitespace()
    tokenizer.enable_truncation(max_length=1)
    path=tmp_path/'tokenizer.json'; tokenizer.save(str(path))
    budget=BGETokenBudget(path,max_tokens=2)
    assert budget.count('hello hello')==2
    with pytest.raises(ValueError): budget.validate('hello hello hello')


def test_sentence_boundaries_preserve_decimal_url_and_closing_quote():
    rows=chunk('# 说明\n价格为3.5元。访问 https://example.com/help 查看。“已经完成！”下一句。',target_chars=30,overlap_chars=5)
    joined='\n'.join(r.answer for r in rows)
    assert '3.5' in joined and 'https://example.com/help' in joined
    assert any('“已经完成！”' in r.answer for r in rows)


def test_budget_splits_combined_sentences_before_soft_target():
    from retailq.knowledge.markdown import chunk_document
    budget=Budget(100)
    rows=chunk_document(document('# 规则\n'+('这是完整知识句子。'*30)),budget,target_chars=600,overlap_chars=20)
    assert len(rows)>1
    assert all(budget.count(embedding_text(r))<=100 for r in rows)
