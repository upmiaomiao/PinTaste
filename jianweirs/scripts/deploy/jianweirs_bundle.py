"""Build a credential-free Linux source release, including the exact DSH source."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tarfile


ROOT=Path(__file__).resolve().parents[2]
EXCLUDED={"node_modules",".git",".venv",".local","__pycache__",".pytest_cache","outputs","output",
          ".cache","target","snapshots",".dsh",".agents",".github","website","docs","benchmarks"}
FOLDERS={"wuyu_harness","omap_api","portal-web","harness-config","scripts","deepseek-harness"}
FILES={"requirements.txt","requirements-wuyu.txt"}


def members():
    for folder in sorted(FOLDERS):
        for parent,dirs,files in os.walk(ROOT/folder):
            dirs[:]=[name for name in dirs if name not in EXCLUDED]
            for name in sorted(files):
                if name.startswith(".env") or name.endswith((".pyc",".tsbuildinfo",".log",".sqlite3",".db")):
                    continue
                path=Path(parent)/name
                if path.is_symlink():
                    continue
                yield path
    for name in FILES:
        yield ROOT/name


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release",required=True)
    parser.add_argument("--destination",type=Path,default=ROOT/"outputs/deployment")
    args=parser.parse_args()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{1,79}",args.release):
        parser.error("release must be a safe identifier")
    args.destination.mkdir(parents=True,exist_ok=True)
    target=args.destination/("jianweirs-"+args.release+".tar.gz")
    paths=list(members())
    manifest={"release":args.release,"private_configuration_included":False,"files":{}}
    with tarfile.open(target,"w:gz",compresslevel=3) as archive:
        for path in paths:
            name=path.relative_to(ROOT).as_posix()
            raw=path.read_bytes()
            manifest["files"][name]=hashlib.sha256(raw).hexdigest()
            entry=archive.gettarinfo(str(path),arcname=name)
            entry.size=len(raw)
            archive.addfile(entry,io.BytesIO(raw))
        raw=json.dumps(manifest,ensure_ascii=False,indent=2).encode()
        entry=tarfile.TarInfo("release-manifest.json");entry.size=len(raw);entry.mode=0o644
        archive.addfile(entry,io.BytesIO(raw))
    digest=hashlib.sha256(target.read_bytes()).hexdigest()
    target.with_suffix(target.suffix+".sha256").write_text(digest+"  "+target.name+"\n",encoding="ascii")
    print(json.dumps({"artifact":str(target),"bytes":target.stat().st_size,"sha256":digest,"files":len(paths)}))


if __name__=="__main__":main()
