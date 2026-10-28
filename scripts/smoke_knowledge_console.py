"""GET-only evidence collector for an existing local knowledge review task."""
import argparse
from contextlib import nullcontext
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

import httpx


def validate_base_url(value):
    url = urlsplit(value)
    if (url.scheme not in ('http','https') or url.hostname not in ('localhost','127.0.0.1','::1')
            or url.username or url.password or url.path not in ('','/') or url.query or url.fragment):
        raise ValueError('only a loopback origin without credentials/path/query is allowed')
    if url.port is not None and not 1 <= url.port <= 65535:
        raise ValueError('invalid port')
    return value.rstrip('/')


def collect_console(base_url, run_id, *, client=None):
    base_url = validate_base_url(base_url)
    if not re.fullmatch('[0-9a-f]{32}',run_id):
        raise ValueError('invalid run ID')
    report = {'run_id':run_id,'captured_at':datetime.now(timezone.utc).isoformat(),
              'evidence':'unavailable','requests':[],'groups':[],'chunks':{},'sources':{},'operations':[],
              'note':'只读的多次GET观测；任务可能在采集期间继续变化，不是跨资源事务快照。'}
    with nullcontext(client) if client is not None else httpx.Client(trust_env=False,follow_redirects=False,timeout=15) as http:
        def get(path):
            response = http.get(base_url + '/api/knowledge' + path)
            report['requests'].append({'method':'GET','path':path,'status':response.status_code})
            if response.status_code != 200:
                raise ValueError(f'GET {path} returned {response.status_code}; no confirmed evidence')
            return response.json()
        try:
            report['summary'] = get('/summary')
            report['run'] = get(f'/runs/{run_id}')
            report['operations'].append(get(f'/operations/{run_id}'))
            offset = 0
            while True:
                page = get(f'/runs/{run_id}/groups?limit=100&offset={offset}')
                for group in page['items']:
                    detail = get(f'/runs/{run_id}/groups/{group["group_id"]}')
                    report['groups'].append(detail)
                    if detail['target_confirmed'] and detail['target_chunk_id'] not in report['chunks']:
                        cid = detail['target_chunk_id']
                        report['chunks'][cid] = get(f'/chunks/{cid}')
                offset += len(page['items'])
                if offset >= page['total']:
                    break
                if not page['items']:
                    raise ValueError('pagination stalled before all groups were captured')
            for source in report['run'].get('sources') or []:
                cid = source['conversation_id']
                report['sources'][cid] = get(f'/sources/{cid}')
            report['evidence'] = 'captured'
        except (httpx.HTTPError,ValueError,KeyError,TypeError):
            report['error'] = '本次只读采集未完整完成；查看requests状态，不推断缺失去向。'
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url',required=True)
    parser.add_argument('--run-id',required=True)
    parser.add_argument('--output',required=True)
    args = parser.parse_args(argv)
    try:
        validate_base_url(args.base_url)
        output = Path(args.output)
        if output.exists():
            raise ValueError('do not overwrite existing evidence')
        report = collect_console(args.base_url,args.run_id)
        output.parent.mkdir(parents=True,exist_ok=True)
        with output.open('x',encoding='utf-8') as stream:
            json.dump(report,stream,ensure_ascii=False,indent=2)
        print(f'{report["evidence"]}: {output}')
        return 0 if report['evidence'] == 'captured' else 2
    except (ValueError,OSError):
        print('只允许本机有效任务与全新输出文件；没有执行任何写API。')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
