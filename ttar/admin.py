import argparse
import json
import os
from pathlib import Path

from .core import Store


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', default='/var/lib/ttar/history.sqlite3')
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('status')
    backup = sub.add_parser('backup')
    backup.add_argument('path')
    setup = sub.add_parser('configure')
    setup.add_argument('--unit', choices=['game', 'match'], required=True)
    setup.add_argument('--k', type=float, default=32)
    retry = sub.add_parser('retry')
    retry.add_argument('id', type=int)
    args = parser.parse_args()
    store = Store(args.db)
    if args.action == 'status':
        print(json.dumps({'jobs': [dict(r) for r in store.db.execute('SELECT status,count(*) count FROM jobs GROUP BY status')],
                          'outbox': [dict(r) for r in store.db.execute('SELECT status,count(*) count FROM outbox GROUP BY status')],
                          'games': store.db.execute('SELECT count(*) FROM games WHERE active=1').fetchone()[0],
                          'drafts': store.db.execute("SELECT count(*) FROM photos WHERE status='draft'").fetchone()[0]}, ensure_ascii=False))
    elif args.action == 'backup':
        path = Path(args.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        store.backup(path)
        os.chmod(path, 0o600)
        print('Database backup complete')
    elif args.action == 'configure':
        if not 0 < args.k <= 200:
            raise SystemExit('K must be > 0 and <= 200')
        with store.transaction():
            if store.db.execute('SELECT count(*) FROM games').fetchone()[0] > 0:
                raise SystemExit('Rating policy is locked after the first confirmation')
            for key, val in [('unit', args.unit), ('k', str(args.k)), ('configured', 'true')]:
                store.db.execute('UPDATE settings SET value=? WHERE key=?', (val, key))
            store.log(0, 'configure', 'settings', None, {'unit': args.unit, 'k': args.k})
        print('Rating policy configured; photos still require confirmation')
    elif args.action == 'retry':
        with store.transaction():
            store.db.execute("UPDATE jobs SET status='pending',attempts=0,ready_at=0 WHERE id=? AND status='failed'", (args.id,))
            store.log(0, 'retry', str(args.id))


if __name__ == '__main__':
    main()
