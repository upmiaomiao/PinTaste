"""Stage a minimal central-platform login-return patch; never activate it."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage",type=Path,required=True)
    args=parser.parse_args()
    stage=args.stage.resolve()
    if not stage.is_relative_to(Path("/opt/wuyu-jianweirs/staging")) or stage.exists():
        raise SystemExit("Use a fresh stage under the product staging folder")
    (stage/"official").mkdir(parents=True,mode=0o700)
    subprocess.run(["docker","cp","wuyu-platform:/app/official/web",str(stage/"official/web")],check=True)
    subprocess.run(["docker","cp","wuyu-platform:/app/official/vite.config.mjs",str(stage/"official/vite.config.mjs")],check=True)
    for name in ("package.json","package-lock.json"):
        shutil.copy2(Path("/root/Project/wuyu-ai-platform")/name,stage/name)
    path=stage/"official/web/main.jsx"
    original_bytes=path.read_bytes()
    before=original_bytes.decode("utf-8")
    initial="useEffect(()=>{api('/me').then(setUser).catch(e=>{setUser(null);if(e.status!==401)setNotice(e.message);});},[]);"
    success="<Auth onSuccess={u=>{setUser(u);setNotice('');}}/>"
    if before.count(initial)!=1 or before.count(success)!=1:
        raise SystemExit("Central frontend source changed; inspect before applying the return patch")
    helper="""function acceptPlatformSession(u){
  const target=new URLSearchParams(window.location.search).get('next');
  if(u?.id&&target==='/jianweirs/'){window.location.replace('/jianweirs/');return;}
  setUser(u);
 }
 """
    after=before.replace(initial,helper+initial.replace(".then(setUser)",".then(acceptPlatformSession)"))
    after=after.replace(success,"<Auth onSuccess={u=>{acceptPlatformSession(u);setNotice('');}}/>")
    path.write_text(after)
    environment={**os.environ,"PATH":"/opt/node/bin:"+os.environ.get("PATH","")}
    subprocess.run(["npm","ci","--ignore-scripts","--no-audit","--fund=false"],cwd=stage,env=environment,check=True)
    subprocess.run(["node","node_modules/vite/bin/vite.js","build","--config","official/vite.config.mjs"],cwd=stage,env=environment,check=True)
    cases=[("?next=%2Fjianweirs%2F",True),("?auth=register&next=/jianweirs/",True),
           ("",False),("?next=https://evil.test",False),("?next=//evil.test",False),
           ("?next=/jianweirs/../../admin",False),("?next=/jianweirs",False)]
    snippet="const cases="+json.dumps(cases)+";for(const [search,want] of cases){if((new URLSearchParams(search).get('next')==='/jianweirs/')!==want)process.exit(1)}"
    subprocess.run(["node","-e",snippet],env=environment,check=True)
    files={str(p.relative_to(stage)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (stage/"official/dist").rglob("*") if p.is_file()}
    report={"status":"staged_not_activated","source":"running wuyu-platform container",
            "changed_source":"official/web/main.jsx","original_sha256":hashlib.sha256(original_bytes).hexdigest(),
            "patched_sha256":hashlib.sha256(after.encode()).hexdigest(),"allowlist_tests":len(cases),
            "return_path":"/jianweirs/","dist_files":files,"main_platform_changed":False}
    (stage/"auth-return-verification.json").write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps({"status":report["status"],"stage":str(stage),"dist_files":len(files),"allowlist_tests":len(cases)}))


if __name__=="__main__":main()
