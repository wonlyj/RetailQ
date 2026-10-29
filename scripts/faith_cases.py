"""List or resolve faithfulness cases; never invokes a model."""
import argparse
import json
from pathlib import Path

from retailq.config import Settings
from retailq.coordination import Coordinator
from retailq.db import Database
from retailq.knowledge.faith_repository import FaithRepository
from retailq.knowledge.faith_store import FaithStore


def main():
    parser = argparse.ArgumentParser(description='忠实度个案台账')
    commands = parser.add_subparsers(dest='command', required=True)
    listing = commands.add_parser('list')
    listing.add_argument('--status', choices=['未解决', '已解决', '无需解决'])
    listing.add_argument('--offset', type=int, default=0)
    listing.add_argument('--limit', type=int, default=20)
    resolve = commands.add_parser('resolve')
    resolve.add_argument('--eval-id', required=True)
    resolve.add_argument('--status', required=True, choices=['未解决', '已解决', '无需解决'])
    resolve.add_argument('--resolution', default='')
    args = parser.parse_args()
    settings = Settings()
    database = Database(settings)
    try:
        coordinator = Coordinator.for_settings(settings)
        store = FaithStore(Path(settings.knowledge_data_dir) / 'faith', coordinator)
        repository = FaithRepository(database, store)
        if args.command == 'resolve':
            rows = [repository.resolve(args.eval_id, args.status, args.resolution)]
        else:
            rows = repository.list_cases(args.status, args.offset, args.limit)
        print(json.dumps([{'eval_id':r.eval_id, 'status':r.status, 'seen_count':r.seen_count,
                           'query':r.query, 'reason':r.reason, 'resolution':r.resolution,
                           'citations':r.citations} for r in rows], ensure_ascii=False, default=str))
    finally:
        database.close()


if __name__ == '__main__':
    main()
