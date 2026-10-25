"""Markdown structure first, complete sentences/rows last. No LLM rewrites."""
import hashlib
import json
from pathlib import Path
import re

from retailq.knowledge.contracts import ChunkDraft, SourceDocument
from retailq.knowledge.text import embedding_text,normalize_text
from retailq.knowledge.token_budget import BGETokenBudget


def load_manifest(path: Path) -> list[SourceDocument]:
    path=path.resolve()
    payload=json.loads(path.read_text(encoding='utf-8'))
    result=[]
    for item in payload['files']:
        target=(path.parent/item['path']).resolve()
        if not target.is_relative_to(path.parent) or target==path.parent:
            raise ValueError('manifest path escapes its source directory')
        kind=item['content_type']
        if kind not in ['faq','policy','manual']:
            raise ValueError('unknown content_type')
        result.append(SourceDocument(target.relative_to(path.parent).as_posix(),target.read_text(encoding='utf-8-sig'),kind,item.get('root_category',target.stem),tuple(item.get('key_sections',[]))))
    return result


def sections(text):
    titles,body,fence=[],[],None
    for line in text.splitlines(keepends=True):
        marker=re.match(r'^\s{0,3}(`{3,}|~{3,})',line)
        if marker:
            token=marker[1]
            if fence is None:
                fence=token
            elif token[0]==fence[0] and len(token)>=len(fence):
                fence=None
            body.append(line)
            continue
        heading=re.match(r'^\s{0,3}(#{1,6})\s+(.+?)\s*$',line) if fence is None else None
        if heading:
            if ''.join(body).strip():
                yield [title for _,title in titles],''.join(body)
            body=[]
            level=len(heading[1])
            titles=[entry for entry in titles if entry[0]<level]
            titles.append((level,re.sub(r'\s+#+\s*$','',heading[2])))
        else:
            body.append(line)
    if ''.join(body).strip():
        yield [title for _,title in titles],''.join(body)


def separator(line):
    cells=re.split(r'(?<!\\)\|',line.strip().strip('|'))
    return bool(cells) and all(re.fullmatch(r'\s*:?-{3,}:?\s*',cell) for cell in cells)


def structures(body):
    lines=body.splitlines(keepends=True)
    i=0
    while i<len(lines):
        if not lines[i].strip():
            i+=1
            continue
        if i+1<len(lines) and '|' in lines[i] and separator(lines[i+1]):
            header=lines[i].rstrip()+'\n'+lines[i+1].rstrip()+'\n'
            i+=2
            rows=[]
            while i<len(lines) and '|' in lines[i] and lines[i].strip():
                rows.append(lines[i].rstrip()+'\n'); i+=1
            yield 'table',(header,rows)
        elif re.match(r'^\s{0,3}(`{3,}|~{3,})',lines[i]):
            marker=re.match(r'^\s{0,3}(`{3,}|~{3,})',lines[i])[1]
            block=[lines[i]]; i+=1
            while i<len(lines):
                line=lines[i]; block.append(line); i+=1
                if re.match(r'^\s{0,3}'+re.escape(marker[0])+'{'+str(len(marker))+r',}\s*$',line): break
            yield 'unit',''.join(block)
        elif re.match(r'^\s*(?:[-*+]|\d+\.)\s+',lines[i]):
            block=[lines[i]]; i+=1
            while i<len(lines) and lines[i].strip() and not re.match(r'^\s*(?:[-*+]|\d+\.)\s+',lines[i]):
                block.append(lines[i]); i+=1
            yield 'unit',''.join(block)
        else:
            block=[lines[i]]; i+=1
            while i<len(lines) and lines[i].strip():
                if (i+1<len(lines) and '|' in lines[i] and separator(lines[i+1])) or re.match(r'^\s{0,3}(`{3,}|~{3,})',lines[i]): break
                block.append(lines[i]); i+=1
            paragraph=''.join(block)
            for sentence in re.findall(r'.+?(?:[。！？][”’\"\x27]*|(?<!\d)[.!?][”’\"\x27]*(?=\s|$)|$)',paragraph,re.S):
                if sentence.strip(): yield 'unit',sentence


def overlap_tail(units,target):
    if not units or not target: return []
    # Nearest complete boundary, never a character slice through a sentence.
    choices=[units[-n:] for n in range(1,len(units)+1)]
    return min(choices,key=lambda group:abs(len(''.join(group))-target))


def pack(units,target,overlap,validate,prefix=''):
    current=[]
    for unit in units:
        validate(prefix+unit)
        candidate=prefix+''.join(current)+unit
        try:
            validate(candidate)
            exceeds_budget=False
        except ValueError:
            exceeds_budget=True
        if current and (len(candidate)>target or exceeds_budget):
            yield normalize_text(prefix+''.join(current))
            current=overlap_tail(current,overlap)
            while current:
                try:
                    validate(prefix+''.join(current)+unit)
                    if len(prefix+''.join(current)+unit)>target: current.pop(0)
                    else: break
                except ValueError:
                    current.pop(0)
        current.append(unit)
    if current: yield normalize_text(prefix+''.join(current))


def chunk_document(document: SourceDocument,budget: BGETokenBudget,*,target_chars: int=600,overlap_chars: int=100) -> list[ChunkDraft]:
    if not 0<=overlap_chars<target_chars:
        raise ValueError('overlap must be nonnegative and smaller than target')
    result=[]
    digest=hashlib.sha256(normalize_text(document.text).encode('utf-8')).hexdigest()
    try:
        for titles,body in sections(normalize_text(document.text)):
            title=titles[-1] if titles else Path(document.key).stem
            category='/'.join(titles[:-1]) or document.root_category
            questions=[title]
            if document.content_type=='faq':
                if not re.search(r'[？?]$',title):
                    raise ValueError('FAQ heading must be a real question')
                aliases=[]
                lines=body.splitlines()
                while lines and (not lines[0].strip() or re.match(r'^问法[：:]',lines[0])):
                    line=lines.pop(0).strip()
                    if line: aliases.append(re.sub(r'^问法[：:]\s*','',line))
                questions+=aliases
                body='\n'.join(lines)
            path='/'.join(titles) or title
            key=any(path==entry or path.startswith(entry+'/') for entry in document.key_sections)
            def make(answer):
                return ChunkDraft(category,'\n'.join(questions),answer,f'{document.key}@{digest} / {path} / 块{len(result)+1}',document.content_type,key)
            def validate(answer): budget.validate(embedding_text(make(normalize_text(answer))))
            pending=[]
            def emit_normal():
                for answer in pack(pending,target_chars,overlap_chars,validate): result.append(make(answer))
                pending.clear()
            for kind,value in structures(body):
                if kind=='unit': pending.append(value)
                else:
                    emit_normal()
                    header,rows=value
                    if not rows:
                        validate(header); result.append(make(normalize_text(header)))
                    else:
                        for answer in pack(rows,target_chars,overlap_chars,validate,header): result.append(make(answer))
            emit_normal()
        if not result: raise ValueError('document has no knowledge content')
    except ValueError as exc:
        raise ValueError(f'{document.key}: {exc}') from None
    return result
