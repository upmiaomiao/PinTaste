"""Create an integrity-checked local backup while the product is idle.

The SQLite backup API includes WAL state. Secrets remain in this server-local,
owner-only directory; this backup must never be added to a source release.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3


def backup(source: Path, target: Path):
    source, target = source.resolve(), target.resolve()
    if not source.is_dir() or target == source or target.is_relative_to(source):
        raise ValueError('Backup destination must be outside shared data')
    if target.exists():
        raise ValueError('Use a new backup directory')
    # Require idle across both stores. Stop the product before this operation
    # for a cross-file snapshot; a single SQLite backup is transaction consistent.
    engine = source/'portal/engine/harness.sqlite3'
    portal = source/'portal/portal.sqlite3'
    runs = {}
    for db_path, table in ((engine,'records'), (portal,'tasks')):
        with sqlite3.connect(db_path.as_uri()+'?mode=ro',uri=True) as db:
            bodies = [json.loads(row[0]) for row in db.execute('SELECT body FROM '+table)]
            if table == 'records':
                runs = {row.get('id'):row for row in bodies}
            if table == 'tasks' and any(row.get('engine_run_id') and row['engine_run_id'] not in runs for row in bodies):
                raise RuntimeError('Unresolved engine references; reconcile before backup')
            states = [runs.get(row.get('engine_run_id'),row).get('status') for row in bodies]
            if any(status in ('planning','running','queued') for status in states):
                raise RuntimeError('Active tasks exist; drain and reconcile status before backup')
    target.mkdir(mode=0o700, parents=True)
    records = {}
    for path in source.rglob('*'):
        if not path.is_file() or path.is_symlink() or path.name.endswith(('-wal','-shm')) or path.name == '.runtime.lock':
            continue
        relative = path.relative_to(source)
        destination = target/relative
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.suffix == '.sqlite3':
            with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as src, sqlite3.connect(destination) as dst:
                src.backup(dst)
                if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise RuntimeError('Database integrity check failed')
        else:
            shutil.copyfile(path, destination)
        destination.chmod(0o600)
        with destination.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        records[relative.as_posix()] = digest
    manifest = target/'backup-manifest.json'
    manifest.write_text(json.dumps({'complete':True, 'files':records},indent=2),encoding='utf8')
    manifest.chmod(0o600)
    return {'backup':str(target), 'files':len(records), 'integrity':'ok'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--target', required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(backup(args.source,args.target)))
