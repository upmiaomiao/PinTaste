"""Run against an assembled public dist, after merging retained release assets."""
from pathlib import Path
import argparse,hashlib,json,re

NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]*-[A-Za-z0-9_-]{8,}\.(?:js|css|png|webp|svg|jpg|jpeg|gif|ico|avif|woff|woff2|ttf|otf|wasm)')


def build(dist):
    dist = Path(dist)
    assert dist.is_dir() and not dist.is_symlink()
    root = dist / 'assets';assert root.is_dir() and not root.is_symlink()
    files = {}
    for path in sorted(root.iterdir()):
        assert not path.is_symlink(), 'PUBLIC_ASSET_LINK_REFUSED'
        if path.is_file() and NAME.fullmatch(path.name):
            raw = path.read_bytes();files[path.name] = {'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw)}
    assert files and len(files) <= 1000, 'PUBLIC_ASSET_MANIFEST_SIZE_INVALID'
    data = (json.dumps({'version':1,'files':files},sort_keys=True,indent=2)+'\n').encode()
    target=dist/'static-assets.json';assert not target.is_symlink()
    target.write_bytes(data)
    return {'files':len(files),'manifest_SHA256':hashlib.sha256(data).hexdigest()}


if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('dist',type=Path)
    print(json.dumps(build(p.parse_args().dist)))
