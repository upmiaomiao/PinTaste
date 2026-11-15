"""Apply only named/container RAM flags to the verified staging renewal script."""
from pathlib import Path
import argparse
import hashlib
import json
import os

TARGET = Path('/opt/wuyu-staging/renew-certificate.sh')
BASE_SHA256 = '00f3320869f8134baf6a26ce521efbea7ed5a45c645f499761d456572e15bb96'
OLD = b'docker run --rm'
NEW = (b'docker run --rm --name wuyu-staging-certbot'
       b' --memory=256000000 --memory-swap=256000000')


def bounded_bytes(raw):
    if NEW in raw:
        assert raw.count(NEW) == 1
        assert hashlib.sha256(raw.replace(NEW, OLD, 1)).hexdigest() == BASE_SHA256
        return raw
    assert hashlib.sha256(raw).hexdigest() == BASE_SHA256, 'Renewal script drift'
    assert raw.count(OLD) == 1
    return raw.replace(OLD, NEW, 1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--write', action='store_true')
    args = parser.parse_args()
    assert TARGET.resolve() == TARGET and not any(p.is_symlink() for p in [TARGET, *TARGET.parents])
    stat = TARGET.stat()
    assert stat.st_uid == 0
    before = TARGET.read_bytes()
    after = bounded_bytes(before)
    if args.write and before != after:
        with TARGET.open('r+b') as stream:
            current = os.fstat(stream.fileno())
            assert (current.st_ino, current.st_dev) == (stat.st_ino, stat.st_dev)
            assert stream.read() == before
            stream.seek(0)
            stream.write(after)
            stream.truncate()
            stream.flush()
            os.fsync(stream.fileno())
    print(json.dumps({'passed': True, 'applied': bool(args.write and before != after),
                      'before_SHA256': hashlib.sha256(before).hexdigest(),
                      'after_SHA256': hashlib.sha256(after).hexdigest(),
                      'same_inode': TARGET.stat().st_ino == stat.st_ino,
                      'container_name': 'wuyu-staging-certbot', 'memory_bytes': 256000000,
                      'total_memory_and_swap_bytes': 256000000,
                      'renewal_executed': False}))


if __name__ == '__main__':
    main()
