"""Preflight or reversibly activate the independent /jianweirs/ service.

Run preflight first; nothing is activated without --activate. Credentials are
read from private files only and never placed in arguments, output, or logs.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import subprocess
import tarfile
import time
import uuid

BASE=Path("/opt/wuyu-jianweirs")
NGINX=Path("/root/Docker/Ngxin/config/conf.d-1.29/ai.conf")
SYSTEMD=Path("/etc/systemd/system")
PUBLIC="https://ai.wuyullm.com"
BEGIN="    # BEGIN WUYU_JIANWEIRS\n"
END="    # END WUYU_JIANWEIRS\n"
UNITS=("jianweirs-science.service","jianweirs.service")


class ActivationError(RuntimeError):pass


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def command(argv, *, data=None, okay=(0,)):
    result=subprocess.run(argv,input=data,capture_output=True)
    if result.returncode not in okay:
        raise ActivationError("Deployment command failed: "+Path(argv[0]).name)
    return result


def atomic_bytes(path, data, mode=0o600):
    path=Path(path);temporary=path.with_name(path.name+".jianweirs-"+uuid.uuid4().hex+".tmp")
    descriptor=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,mode)
    try:
        with os.fdopen(descriptor,"wb") as stream:stream.write(data)
        os.replace(temporary,path)
    finally:temporary.unlink(missing_ok=True)


def nginx_configuration(original):
    block=BEGIN+'''    location = /jianweirs { return 308 /jianweirs/; }
    location ^~ /jianweirs/ {
        proxy_pass http://172.17.0.1:8170/;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header Cookie $http_cookie;
        proxy_set_header Authorization "";
        proxy_set_header X-Wuyu-Identity-Key "";
        proxy_set_header X-Wuyu-Internal-Key "";
        proxy_set_header X-Wuyu-Tenant-Access "";
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Connection "";
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
        proxy_connect_timeout 10s;
        proxy_buffering off;
        proxy_request_buffering off;
        client_max_body_size 110m;
        proxy_redirect off;
    }
'''+END+"\n"
    if BEGIN in original:
        if original.count(BEGIN)!=1 or original.count(END)!=1:
            raise ActivationError("Unexpected existing product nginx markers")
        first=original.index(BEGIN);last=original.index(END,first)+len(END)
        return original[:first]+block.rstrip("\n")+"\n"+original[last:]
    if re.search(r"location[^\n]*?/jianweirs",original):
        raise ActivationError("An unmanaged /jianweirs nginx route already exists")
    marker="    location @wuyu_work_login"
    if original.count(marker)!=1:
        raise ActivationError("The expected HTTPS insertion point is not unique")
    before=original[:original.index(marker)]
    if not re.search(r"listen\s+443\s+ssl",before):
        raise ActivationError("HTTPS server insertion point was not found")
    return original.replace(marker,block+marker,1)


def private_configuration(folder, *, require_empty_seed=True):
    from cryptography.fernet import Fernet
    from dotenv import dotenv_values
    folder=Path(folder)
    env=dotenv_values(folder/"server.env")
    database=folder/"portal/portal.sqlite3";key=folder/"portal/.config.key"
    if any(not p.is_file() or p.is_symlink() for p in (database,key,folder/"server.env")):
        raise ActivationError("Private state files must be regular files")
    try:
        with sqlite3.connect(database.resolve().as_uri()+"?mode=ro",uri=True) as db:
            ciphertext=db.execute("SELECT ciphertext FROM config WHERE id=1").fetchone()[0]
            counts={table:db.execute("SELECT count(*) FROM "+table).fetchone()[0]
                    for table in ("users","sessions","ownership","tasks")}
        cfg=json.loads(Fernet(key.read_bytes()).decrypt(ciphertext))
    except Exception:
        raise ActivationError("Private configuration key does not decrypt the prepared configuration") from None
    worker=env.get("OMAP_WORKER_API_KEY","")
    if not worker or not hmac.compare_digest(worker,cfg.get("data",{}).get("api_key","")):
        raise ActivationError("The science worker key and encrypted data key do not match")
    if cfg["data"].get("base_url")!="http://127.0.0.1:8171" or cfg["data"].get("mode")!="worker":
        raise ActivationError("The prepared science endpoint must be the independent loopback worker")
    if env.get("WUYU_AUTH_MODE")!="wuyu" or env.get("WUYU_IDENTITY_URL")!="http://127.0.0.1:8920":
        raise ActivationError("The prepared identity boundary is not the central Wuyu session endpoint")
    if require_empty_seed and any(counts.values()):
        raise ActivationError("Only a zero-account, zero-session configuration seed may be deployed")
    if require_empty_seed and (not cfg.get("unified_data",{}).get("enabled") or not cfg["unified_data"].get("api_key")):
        raise ActivationError("The prepared unified imagery configuration is incomplete")
    return {"fernet_key_matches":True,"worker_key_matches":True,"empty_account_seed":not any(counts.values()),
            "data_endpoint":"http://127.0.0.1:8171",
            "unified_configured":bool(cfg.get("unified_data",{}).get("api_key"))}


def archive_manifest(path, expected_hash=None):
    path=Path(path)
    if expected_hash and not hmac.compare_digest(sha(path),expected_hash):
        raise ActivationError("Source archive transfer hash mismatch")
    with tarfile.open(path) as archive:
        members=archive.getmembers()
        if len(members)>100000 or sum(item.size for item in members)>2*1024**3:
            raise ActivationError("Source archive exceeds the deployment size budget")
        names=set()
        for item in members:
            name=Path(item.name)
            if (name.is_absolute() or item.name.startswith("/") or "\\" in item.name or re.match(r"^[A-Za-z]:",item.name)
                    or ".." in name.parts or item.issym() or item.islnk() or item.size>128*1024**2
                    or not (item.isfile() or item.isdir()) or item.name in names
                    or any(part in {".local",".venv","node_modules","outputs","output"} for part in name.parts)
                    or name.name in {".config.key","vault.key","ACCESS.txt","cluster.json","ngrok.yml"}
                    or name.name.startswith(".env")):
                raise ActivationError("Source archive contains an unsafe or private member")
            names.add(item.name)
        stream=archive.extractfile("release-manifest.json")
        manifest=json.loads(stream.read()) if stream else {}
        files=manifest.get("files",{})
        if set(files)|{"release-manifest.json"}!={item.name for item in members if item.isfile()}:
            raise ActivationError("Source archive and manifest members differ")
        for name,digest in files.items():
            payload=archive.extractfile(name)
            if not payload or hashlib.sha256(payload.read()).hexdigest()!=digest:
                raise ActivationError("Source archive member hash mismatch")
    return manifest


def container_source_hash():
    snippet="from pathlib import Path;import hashlib;print(hashlib.sha256(Path('/app/official/web/main.jsx').read_bytes()).hexdigest())"
    return command(["docker","exec","wuyu-platform","python","-c",snippet]).stdout.decode().strip()


def unit_state(name):
    active=command(["systemctl","is-active",name],okay=(0,3,4)).stdout.decode().strip()=="active"
    enabled=command(["systemctl","is-enabled",name],okay=(0,1,3,4)).stdout.decode().strip()=="enabled"
    path=SYSTEMD/name
    content=path.read_bytes() if path.exists() else None
    if content and b"/opt/wuyu-jianweirs/current/" not in content:
        raise ActivationError("Refusing to replace an unmanaged systemd service")
    return {"active":active,"enabled":enabled,"existed":content is not None},content


def test_nginx_candidate(changed):
    """Test a private cloned config tree without touching the active include."""
    token=uuid.uuid4().hex
    folder=BASE/"staging"/("nginx-check-"+token)
    folder.mkdir(mode=0o700)
    command(["docker","cp","nginx-1.29:/etc/nginx/nginx.conf",str(folder/"nginx.conf")])
    command(["docker","cp","nginx-1.29:/etc/nginx/conf.d",str(folder/"conf.d")])
    remote="/tmp/jianweirs-nginx-check-"+token
    raw=(folder/"nginx.conf").read_text()
    pattern=r"include\s+/etc/nginx/conf\.d/\*\.conf;"
    if len(re.findall(pattern,raw))!=1:raise ActivationError("The nginx include tree cannot be safely cloned")
    (folder/"nginx.conf").write_text(re.sub(pattern,"include "+remote+"/conf.d/*.conf;",raw,count=1))
    (folder/"conf.d/ai.conf").write_text(changed)
    command(["docker","cp",str(folder),"nginx-1.29:"+remote])
    command(["docker","exec","nginx-1.29","nginx","-t","-c",remote+"/nginx.conf"])


def preflight(args):
    private=private_configuration(args.private_state)
    if not (args.runtime_release/".venv/bin/python").is_file() or not (args.runtime_release/"deepseek-harness/node_modules/.modules.yaml").is_file():
        raise ActivationError("Prepared Linux Python or DSH dependencies are absent")
    native=args.runtime_release/"deepseek-harness/native/system/packages/linux-x64/bin/glibc/system.node"
    if not native.is_file():raise ActivationError("The Linux DSH file-lock binding is not built")
    auth=json.loads((args.auth_stage/"auth-return-verification.json").read_text())
    if auth.get("status")!="staged_not_activated" or container_source_hash()!=auth.get("original_sha256"):
        raise ActivationError("The central frontend changed after the login-return patch was staged")
    if sha(args.auth_stage/"official/web/main.jsx")!=auth.get("patched_sha256"):
        raise ActivationError("The staged central frontend source hash differs")
    for name,digest in auth["dist_files"].items():
        if sha(args.auth_stage/name)!=digest:raise ActivationError("The staged central frontend build hash differs")
    current=BASE/"current"
    if current.exists() and not current.is_symlink():raise ActivationError("The current release pointer is not a managed symlink")
    for name in UNITS:
        state,_=unit_state(name)
        port=8170 if name=="jianweirs.service" else 8171
        with socket.socket() as sock:
            occupied=sock.connect_ex(("172.17.0.1" if port==8170 else "127.0.0.1",port))==0
        if occupied and not state["active"]:raise ActivationError("A product port is occupied by an unmanaged process")
    shared=BASE/"shared/portal"
    if shared.exists():
        if not (shared/".config.key").exists() or not hmac.compare_digest((shared/".config.key").read_bytes(),(args.private_state/"portal/.config.key").read_bytes()):
            raise ActivationError("Existing product state has a different key; it must not be overwritten")
    original=NGINX.read_text();changed=nginx_configuration(original)
    manifest=archive_manifest(args.archive,args.sha256) if args.archive else None
    if manifest:
        for name,digest in manifest["files"].items():
            if name.startswith("deepseek-harness/") and sha(args.runtime_release/name)!=digest:
                raise ActivationError("The prepared DSH runtime differs from the release source snapshot")
    command(["docker","exec","nginx-1.29","nginx","-t"])
    test_nginx_candidate(changed)
    return {"private_configuration":private,"runtime_ready":True,"auth_return_ready":True,
            "nginx_current_valid":True,"nginx_candidate_valid":True,"nginx_change_ready":changed!=original,
            "source_archive_verified":manifest is not None},manifest


def product_current():
    current=BASE/'current'
    if not current.is_symlink():raise ActivationError('Product update requires a managed current release')
    target=current.resolve(strict=True)
    if target.parent!=(BASE/'releases').resolve():
        raise ActivationError('Current product release is outside the managed release directory')
    return target


def product_idle_snapshot():
    """Read every task and actual engine run, without constructing/recovering a Store."""
    portal=BASE/'shared/portal/portal.sqlite3'
    engine=BASE/'shared/portal/engine/harness.sqlite3'
    terminal={'succeeded','completed','failed','planning_failed','interrupted','cancelled'}
    pending={'planning','queued','running'}
    try:
        with sqlite3.connect(portal.resolve().as_uri()+'?mode=ro',uri=True) as db:
            db.execute('BEGIN')
            tasks=[json.loads(row[0]) for row in db.execute('SELECT body FROM tasks')]
        with sqlite3.connect(engine.resolve().as_uri()+'?mode=ro',uri=True) as db:
            db.execute('BEGIN')
            runs={row[0]:json.loads(row[1]) for row in db.execute("SELECT id,body FROM records WHERE kind='run'")}
        unresolved=0
        missing=0
        for task in tasks:
            run_id=task.get('engine_run_id')
            if run_id:
                if run_id not in runs:
                    missing+=1
                    continue
                status=runs[run_id].get('status')
            else:status=task.get('status')
            if status in pending or status not in terminal:unresolved+=1
        engine_pending=sum(run.get('status') in pending or run.get('status') not in terminal for run in runs.values())
        task_ids=sorted(str(task['id']) for task in tasks)
    except Exception:
        raise ActivationError('The complete product task/engine state could not be read safely') from None
    snapshot={'portal_tasks':len(tasks),'engine_runs':len(runs),'pending_portal_tasks':unresolved,
              'pending_engine_runs':engine_pending,'missing_engine_records':missing,
              'task_id_digest':hashlib.sha256(json.dumps(task_ids).encode()).hexdigest()}
    if unresolved or engine_pending or missing:
        raise ActivationError(f'Product is busy or unresolved: tasks={unresolved}, engine_runs={engine_pending}, missing={missing}; wait for current user tasks to finish')
    return snapshot


def product_boundary():
    """Fingerprints only; never back up or replace shared credentials or accounts."""
    paths={'nginx':NGINX,'shared_key':BASE/'shared/portal/.config.key','server_env':BASE/'shared/server.env'}
    paths.update({name:SYSTEMD/name for name in UNITS})
    try:
        result={name:sha(path) for name,path in paths.items()}
        with sqlite3.connect((BASE/'shared/portal/portal.sqlite3').resolve().as_uri()+'?mode=ro',uri=True) as db:
            ciphertext=db.execute('SELECT ciphertext FROM config WHERE id=1').fetchone()[0]
        result['encrypted_config']=hashlib.sha256(ciphertext).hexdigest()
        result['main_frontend']=container_source_hash()
        return result
    except Exception:
        raise ActivationError('Protected product or central boundary could not be verified') from None


def assert_product_boundary(expected):
    if product_boundary()!=expected:
        raise ActivationError('Protected configuration, service units, or central platform changed; nothing will be overwritten')


def verify_product_compatibility(args,manifest,current):
    old=json.loads((current/'release-manifest.json').read_text())['files']
    files=manifest['files']
    dependency_files={'requirements.txt','requirements-wuyu.txt','scripts/deploy/jianweirs_requirements.lock'}
    science_launcher='scripts/deploy/jianweirs_science_server.py'
    science={name for name in old.keys()|files.keys() if name.startswith('omap_api/') or name==science_launcher}
    separate_python = getattr(args, 'product_python_runtime', None)
    for name in science | (set() if separate_python else dependency_files):
        if name not in old or name not in files or old[name]!=files[name] or sha(current/name)!=old[name]:
            raise ActivationError('A science or dependency change needs a separate reviewed runtime update')
    for name,digest in files.items():
        if name.startswith('deepseek-harness/') and sha(args.runtime_release/name)!=digest:
            raise ActivationError('The prepared DSH runtime differs from the update source snapshot')
    runtime=args.runtime_release.resolve(strict=True)
    current_python=(current/'.venv').resolve(strict=True)
    managed_python=current_python==runtime/'.venv' or current_python.is_relative_to(BASE/'runtimes')
    if ((not separate_python and not managed_python)
            or (current/'deepseek-harness').resolve()!=runtime/'deepseek-harness'):
        raise ActivationError('The product update must retain the current validated dependency runtime')
    if separate_python:
        verify_product_python_runtime(args, manifest)


def verify_product_python_runtime(args, manifest):
    """An explicit, separately tested environment; never mutate the old runtime."""
    root = args.product_python_runtime.resolve(strict=True)
    if not root.is_relative_to(BASE/'runtimes') or not (root/'bin/python').is_file():
        raise ActivationError('Product Python must be an isolated prepared runtime below runtimes/')
    proof = json.loads((root/'runtime-verification.json').read_text())
    expected_lock = manifest['files'].get('scripts/deploy/jianweirs_requirements.lock')
    if (proof.get('requirements_sha256') != expected_lock or proof.get('pip_check') != 'passed'
            or proof.get('dependency_audit_vulnerabilities') != 0
            or not proof.get('tests_passed') or proof.get('source_archive_sha256') != args.sha256):
        raise ActivationError('The separate Python runtime lacks matching audit and test evidence')
    code = 'import importlib.metadata as m,json; print(json.dumps({d.metadata["Name"].lower().replace("_","-"):d.version for d in m.distributions()}))'
    actual = json.loads(command([str(root/'bin/python'),'-c',code]).stdout)
    if actual != proof.get('packages'):
        raise ActivationError('The prepared product Python packages changed after validation')
    command([str(root/'bin/python'),'-m','pip','check'])
    return root


def product_freezer():
    group=command(['systemctl','show','-p','ControlGroup','--value','jianweirs.service']).stdout.decode().strip()
    if group!='/system.slice/jianweirs.service' or not (Path('/sys/fs/cgroup')/group.lstrip('/')/'cgroup.freeze').is_file():
        raise ActivationError('A scoped product cgroup freezer is required for a safe idle update')


def product_update_preflight(args):
    if not args.archive or not args.sha256:
        raise ActivationError('Product update preflight requires the final source archive and SHA256')
    current=product_current()
    if current.name==args.release or (BASE/'releases'/args.release).exists():
        raise ActivationError('The new immutable product release must have a unique name')
    private=private_configuration(BASE/'shared',require_empty_seed=False)
    manifest=archive_manifest(args.archive,args.sha256)
    if manifest.get('release')!=args.release:
        raise ActivationError('The source manifest release does not match the requested update')
    if not (args.runtime_release/'.venv/bin/python').is_file() or not (args.runtime_release/'deepseek-harness/node_modules/.modules.yaml').is_file():
        raise ActivationError('Validated product dependencies are missing')
    native=args.runtime_release/'deepseek-harness/native/system/packages/linux-x64/bin/glibc/system.node'
    if not native.is_file():raise ActivationError('The validated native DSH binding is missing')
    verify_product_compatibility(args,manifest,current)
    states={name:unit_state(name)[0] for name in UNITS}
    if not all(state['active'] and state['existed'] for state in states.values()):
        raise ActivationError('Both existing managed product services must be active before a portal-only update')
    product_freezer()
    idle=product_idle_snapshot()
    return {'mode':'product_update','current_release':str(current),'source_archive_verified':True,
            'shared_configuration_verified':private,'dependencies_unchanged':not bool(getattr(args,'product_python_runtime',None)),
            'isolated_product_python':str(args.product_python_runtime) if getattr(args,'product_python_runtime',None) else None,
            'science_source_unchanged':True,
            'science_restart_required':bool(getattr(args,'product_python_runtime',None)),
            'idle':idle,'protected_boundary':product_boundary(),'services_before':states},manifest


def install_release(args,manifest):
    target=BASE/"releases"/args.release
    if target.exists():raise ActivationError("An immutable release with this name already exists")
    incoming=target.with_name("."+target.name+".incoming-"+uuid.uuid4().hex)
    incoming.mkdir(mode=0o700)
    with tarfile.open(args.archive) as archive:archive.extractall(incoming,filter="data")
    for name,digest in manifest["files"].items():
        if sha(incoming/name)!=digest:raise ActivationError("Extracted source member hash mismatch")
    os.rename(incoming/"deepseek-harness",incoming/"deepseek-source-snapshot")
    (incoming/"deepseek-harness").symlink_to(args.runtime_release/"deepseek-harness",target_is_directory=True)
    python_runtime=getattr(args,'product_python_runtime',None)
    if python_runtime is None:
        python_runtime=(product_current()/'.venv').resolve(strict=True) if getattr(args,'product_update',False) else args.runtime_release/'.venv'
    (incoming/".venv").symlink_to(python_runtime,target_is_directory=True)
    os.replace(incoming,target)
    return target


def install_private(args):
    shared=BASE/"shared";shared.mkdir(mode=0o700,exist_ok=True)
    portal=shared/"portal"
    if not portal.exists():
        shutil.copytree(args.private_state/"portal",portal)
        portal.chmod(0o700)
        for path in portal.iterdir():path.chmod(0o600)
    canonical=shared/"server.env"
    if not canonical.exists():
        raw=(args.private_state/"server.env").read_bytes().rstrip(b"\r\n")
        raw+=b"\nWUYU_PROXY_IPS=172.20.0.2,172.29.0.3,127.0.0.1,172.17.0.1\n"
        atomic_bytes(canonical,raw)
    for name in ("service.env","science.env"):
        path=shared/name
        if not path.exists():path.symlink_to("server.env")


def point_current(target):
    temporary=BASE/(".current-"+uuid.uuid4().hex)
    temporary.symlink_to(target,target_is_directory=True)
    os.replace(temporary,BASE/"current")


def ready(url,*,expected=200,seconds=30):
    import httpx
    from urllib.parse import urlsplit
    parsed=urlsplit(url)
    if (parsed.scheme!='http' or parsed.hostname not in {'127.0.0.1','172.17.0.1'}
            or parsed.port not in {8170,8171} or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise ActivationError("Readiness probes must target a private product service")
    deadline=time.monotonic()+seconds
    last_result='unavailable'
    # This host SNATs its own docker0-destination traffic through eth0. An
    # explicit loopback source keeps the forwarded HTTPS header within the
    # existing exact trusted-proxy allowlist, without trusting another subnet.
    with httpx.Client(transport=httpx.HTTPTransport(local_address='127.0.0.1'),
                      timeout=3,trust_env=False,follow_redirects=False) as client:
        while time.monotonic()<deadline:
            try:
                response=client.get(url,headers={"Host":"ai.wuyullm.com","X-Forwarded-Proto":"https"})
                last_result='HTTP '+str(response.status_code)
                if response.status_code==expected:return response
            except httpx.HTTPError as error:
                last_result=type(error).__name__
            time.sleep(.5)
    raise ActivationError(f"Readiness failed on product port {parsed.port} {parsed.path}: {last_result}; expected HTTP {expected}")


def apply_main_frontend(args,transaction):
    before=container_source_hash()
    auth=json.loads((args.auth_stage/"auth-return-verification.json").read_text())
    if before!=auth["original_sha256"]:raise ActivationError("Central frontend changed before the atomic patch")
    command(["docker","cp","wuyu-platform:/app/official/web/main.jsx",str(transaction/"main.jsx.before")])
    command(["docker","cp","wuyu-platform:/app/official/dist",str(transaction/"central-dist.before")])
    incoming="/app/official/.jianweirs-incoming-"+transaction.name
    command(["docker","exec","wuyu-platform","mkdir","-p",incoming])
    command(["docker","cp",str(args.auth_stage/"official/web/main.jsx"),"wuyu-platform:"+incoming+"/main.jsx"])
    command(["docker","cp",str(args.auth_stage/"official/dist"),"wuyu-platform:"+incoming+"/dist"])
    script='''from pathlib import Path
import os,shutil,sys,uuid
source=Path(sys.argv[1]);target=Path('/app/official/dist')
def copy_atomic(src,dst):
 dst.parent.mkdir(parents=True,exist_ok=True);tmp=dst.with_name(dst.name+'.jianweirs-'+uuid.uuid4().hex);shutil.copy2(src,tmp);os.replace(tmp,dst)
for path in (source/'dist').rglob('*'):
 if path.is_file() and path.name!='index.html':copy_atomic(path,target/path.relative_to(source/'dist'))
copy_atomic(source/'main.jsx',Path('/app/official/web/main.jsx'))
copy_atomic(source/'dist/index.html',target/'index.html')
'''
    command(["docker","exec","-i","wuyu-platform","python","-",incoming],data=script.encode())


def restore_main_frontend(transaction):
    incoming="/app/official/.jianweirs-rollback-"+transaction.name
    command(["docker","exec","wuyu-platform","mkdir","-p",incoming])
    command(["docker","cp",str(transaction/"main.jsx.before"),"wuyu-platform:"+incoming+"/main.jsx"])
    command(["docker","cp",str(transaction/"central-dist.before"),"wuyu-platform:"+incoming+"/dist"])
    script='''from pathlib import Path
import os,shutil,sys,uuid
s=Path(sys.argv[1]);t=Path('/app/official/dist')
def put(a,b):
 b.parent.mkdir(parents=True,exist_ok=True);p=b.with_name(b.name+'.restore-'+uuid.uuid4().hex);shutil.copy2(a,p);os.replace(p,b)
for p in (s/'dist').rglob('*'):
 if p.is_file() and p.name!='index.html':put(p,t/p.relative_to(s/'dist'))
put(s/'main.jsx',Path('/app/official/web/main.jsx'));put(s/'dist/index.html',t/'index.html')
'''
    command(["docker","exec","-i","wuyu-platform","python","-",incoming],data=script.encode())


def public_probes():
    import httpx
    checks=[]
    with httpx.Client(timeout=20,trust_env=False,follow_redirects=False) as client:
        for path,wanted in (("/jianweirs",308),("/jianweirs/",303),("/jianweirs/health",200),
                            ("/jianweirs/portal/api/session",200),("/jianweirs/portal/api/tasks",401),("/",200)):
            response=client.get(PUBLIC+path)
            if response.status_code!=wanted:raise ActivationError("Public routing/authentication probe failed")
            if path=="/jianweirs/" and response.headers.get("location")!=PUBLIC+"/?next=%2Fjianweirs%2F":
                raise ActivationError("Anonymous product access did not return to central login")
            if path.endswith("/session"):
                body=response.json()
                if body.get("user") is not None or body.get("bootstrap_required") is not False or body.get("auth",{}).get("mode")!="wuyu":
                    raise ActivationError("The public product identity boundary is not enabled")
            checks.append({"path":path,"status_code":wanted})
    return checks


def product_health():
    ready('http://127.0.0.1:8171/health')
    ready('http://172.17.0.1:8170/health')
    session=ready('http://172.17.0.1:8170/portal/api/session').json()
    if (session.get('auth',{}).get('mode')!='wuyu' or session.get('bootstrap_required') is not False
            or session.get('user') is not None or session.get('csrf_token') is not None):
        raise ActivationError('The updated product did not retain the anonymous central identity boundary')


def same_idle_work(before,after):
    if any(before[key]!=after[key] for key in ('portal_tasks','engine_runs','task_id_digest')):
        raise ActivationError('New work arrived during the idle update window; retry after users stop submitting')


def product_update(args,manifest,summary):
    """Update only the product pointer/process; never call the initial activation path."""
    previous=Path(summary['current_release'])
    if product_current()!=previous:raise ActivationError('The current release changed after product update preflight')
    boundary=summary['protected_boundary']
    assert_product_boundary(boundary)
    transaction=BASE/'shared/transactions'/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8])
    transaction.mkdir(parents=True,mode=0o700)
    record={'mode':'product_update','release':args.release,'previous_current':str(previous),
            'new_release':str(BASE/'releases'/args.release),'services_before':summary['services_before'],
            'protected_boundary':boundary,'idle_before':summary['idle'],'status':'preparing',
            'science_restart_required':bool(getattr(args,'product_python_runtime',None)),
            'configuration_and_identity_preserved':True}
    atomic_bytes(transaction/'transaction.json',json.dumps(record,indent=2).encode())
    frozen=False;stopped=False;pointer_changed=False;science_stopped=False
    try:
        target=install_release(args,manifest)
        # Unpacking can take time. Users may finish or submit work during it.
        idle=product_idle_snapshot()
        assert_product_boundary(boundary)
        if product_current()!=previous:raise ActivationError('Current product pointer changed during source installation')
        command(['systemctl','freeze','jianweirs.service']);frozen=True
        # A paused cgroup cannot admit another task while this second snapshot
        # checks actual engine state. systemd automatically thaws on stop.
        frozen_idle=product_idle_snapshot()
        same_idle_work(idle,frozen_idle)
        command(['systemctl','stop','jianweirs.service']);stopped=True;frozen=False
        same_idle_work(frozen_idle,product_idle_snapshot())
        # Both launchers use current/.venv. A reviewed dependency upgrade must
        # switch the loopback-only worker too, avoiding mixed loaded libraries.
        if record['science_restart_required']:
            command(['systemctl','stop','jianweirs-science.service']);science_stopped=True
        if getattr(args,'backup_state',False):
            destination=BASE/'backups'/transaction.name
            result=command([str(target/'.venv/bin/python'),str(target/'scripts/deploy/jianweirs_backup.py'),
                            '--source',str(BASE/'shared'),'--target',str(destination)])
            record['state_backup']=json.loads(result.stdout)
        assert_product_boundary(boundary)
        point_current(target);pointer_changed=True
        if science_stopped:
            command(['systemctl','start','jianweirs-science.service'])
            ready('http://127.0.0.1:8171/health')
        command(['systemctl','start','jianweirs.service'])
        product_health()
        record['public_probes']=public_probes()
        assert_product_boundary(boundary)
        if {name:unit_state(name)[0] for name in UNITS}!=summary['services_before']:
            raise ActivationError('Managed product service states changed unexpectedly')
        record.update(status='product_updated',public_url=PUBLIC+'/jianweirs/',idle_at_switch=frozen_idle)
    except BaseException as error:
        rollback=[]
        if frozen:
            try:command(['systemctl','thaw','jianweirs.service']);rollback.append('product_thawed')
            except Exception:rollback.append('product_thaw_manual_review_required')
        if stopped or pointer_changed:
            try:
                if product_current() not in {previous,BASE/'releases'/args.release}:
                    raise ActivationError('Another operator changed the current product release')
                command(['systemctl','stop','jianweirs.service'])
                if science_stopped:command(['systemctl','stop','jianweirs-science.service'])
                if pointer_changed:point_current(previous)
                if science_stopped:
                    command(['systemctl','start','jianweirs-science.service'])
                    ready('http://127.0.0.1:8171/health')
                if summary['services_before']['jianweirs.service']['active']:
                    command(['systemctl','start','jianweirs.service'])
                    product_health()
                rollback.append('previous_product_process_restored')
            except Exception:rollback.append('product_process_manual_review_required')
        record.update(status='product_update_rolled_back',failure=type(error).__name__,rollback=rollback)
        atomic_bytes(transaction/'transaction.json',json.dumps(record,indent=2).encode())
        raise ActivationError('Product update failed; product-only rollback recorded at '+str(transaction)) from None
    atomic_bytes(transaction/'transaction.json',json.dumps(record,indent=2).encode())
    return record|{'transaction':str(transaction)}


def rollback_product_transaction(transaction,record):
    """Manual product rollback keeps central files, science, and private data intact."""
    previous=Path(record['previous_current']).resolve(strict=True)
    target=Path(record['new_release']).resolve(strict=True)
    if previous.parent!=(BASE/'releases').resolve() or target.parent!=previous.parent:
        raise ActivationError('Product rollback release is outside the managed directory')
    if product_current()==previous:
        return {'status':'already_at_previous_product','transaction':str(transaction)}
    if product_current()!=target:raise ActivationError('A different product release is current; rollback refused')
    assert_product_boundary(record['protected_boundary'])
    product_freezer()
    idle=product_idle_snapshot()
    frozen=False;stopped=False;changed=False;science_stopped=False
    try:
        command(['systemctl','freeze','jianweirs.service']);frozen=True
        frozen_idle=product_idle_snapshot();same_idle_work(idle,frozen_idle)
        command(['systemctl','stop','jianweirs.service']);stopped=True;frozen=False
        same_idle_work(frozen_idle,product_idle_snapshot())
        if record.get('science_restart_required'):
            command(['systemctl','stop','jianweirs-science.service']);science_stopped=True
        point_current(previous);changed=True
        if science_stopped:
            command(['systemctl','start','jianweirs-science.service'])
            ready('http://127.0.0.1:8171/health')
        command(['systemctl','start','jianweirs.service'])
        product_health();checks=public_probes()
        assert_product_boundary(record['protected_boundary'])
    except BaseException:
        if frozen:command(['systemctl','thaw','jianweirs.service'])
        if stopped or changed:
            if product_current() not in {previous,target}:raise ActivationError('Current pointer changed during product rollback') from None
            command(['systemctl','stop','jianweirs.service'])
            if science_stopped:command(['systemctl','stop','jianweirs-science.service'])
            if changed:point_current(target)
            if science_stopped:
                command(['systemctl','start','jianweirs-science.service'])
                ready('http://127.0.0.1:8171/health')
            command(['systemctl','start','jianweirs.service'])
        raise ActivationError('Product rollback did not pass verification; current product was restored') from None
    record.update(status='product_rolled_back_manually',rollback_public_probes=checks)
    atomic_bytes(transaction/'transaction.json',json.dumps(record,indent=2).encode())
    return {'status':record['status'],'transaction':str(transaction),'configuration_and_identity_preserved':True}


def rollback_transaction(transaction):
    """Recover a completed/interrupted transaction without removing user data."""
    transaction=Path(transaction).resolve()
    if not transaction.is_relative_to((BASE/"shared/transactions").resolve()):
        raise ActivationError("Rollback transaction is outside this product")
    record=json.loads((transaction/"transaction.json").read_text())
    if record.get('mode')=='product_update':return rollback_product_transaction(transaction,record)
    current=BASE/"current"
    pointer=os.readlink(current) if current.is_symlink() else None
    if pointer not in {record.get("new_release"),record.get("previous_current"),None}:
        raise ActivationError("Another release is current; do not overwrite it during rollback")
    config=NGINX.read_bytes();digest=hashlib.sha256(config).hexdigest()
    if digest not in {record.get("new_nginx_sha256"),record.get("old_nginx_sha256")}:
        raise ActivationError("Nginx changed outside this transaction; manual review is required")
    source_hash=container_source_hash()
    if source_hash not in {record.get("frontend_original_sha256"),record.get("frontend_patched_sha256")}:
        raise ActivationError("Central frontend changed outside this transaction; manual review is required")
    if digest==record.get("new_nginx_sha256"):
        atomic_bytes(NGINX,(transaction/"nginx.before").read_bytes(),0o644)
        command(["docker","exec","nginx-1.29","nginx","-t"])
        command(["docker","exec","nginx-1.29","nginx","-s","reload"])
    if (transaction/"main.jsx.before").exists() and (transaction/"central-dist.before/index.html").exists():
        restore_main_frontend(transaction)
    states=record["services_before"]
    for name in reversed(UNITS):
        existing=SYSTEMD/name
        if existing.exists() and b"/opt/wuyu-jianweirs/current/" not in existing.read_bytes():
            raise ActivationError("A product service was changed outside the transaction")
        command(["systemctl","stop",name],okay=(0,5))
        backup=transaction/(name+".before")
        if backup.exists():atomic_bytes(existing,backup.read_bytes(),0o644)
        else:
            command(["systemctl","disable",name],okay=(0,1));existing.unlink(missing_ok=True)
    previous=record.get("previous_current")
    if previous:point_current(Path(previous))
    elif current.is_symlink():current.unlink()
    command(["systemctl","daemon-reload"])
    for name,state in states.items():
        if state["enabled"]:command(["systemctl","enable",name])
        elif state["existed"]:command(["systemctl","disable",name],okay=(0,1))
        if state["active"]:command(["systemctl","start",name])
    record.update(status="rolled_back_manually",private_state_retained_on_rollback=True)
    atomic_bytes(transaction/"transaction.json",json.dumps(record,indent=2).encode())
    return {"status":record["status"],"transaction":str(transaction),"private_state_retained":True}


def activate(args,manifest):
    transaction=BASE/"shared/transactions"/(datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")+"-"+uuid.uuid4().hex[:8])
    transaction.mkdir(parents=True,mode=0o700)
    original=NGINX.read_bytes();changed=nginx_configuration(original.decode()).encode()
    atomic_bytes(transaction/"nginx.before",original)
    old_current=os.readlink(BASE/"current") if (BASE/"current").is_symlink() else None
    states={};written_units=[];frontend_changed=False;nginx_changed=False;pointer_changed=False
    for name in UNITS:
        state,content=unit_state(name);states[name]=state
        if content is not None:atomic_bytes(transaction/(name+".before"),content)
    auth=json.loads((args.auth_stage/"auth-return-verification.json").read_text())
    record={"release":args.release,"previous_current":old_current,"services_before":states,
            "new_release":str(BASE/"releases"/args.release),
            "old_nginx_sha256":hashlib.sha256(original).hexdigest(),"new_nginx_sha256":hashlib.sha256(changed).hexdigest(),
            "frontend_original_sha256":auth["original_sha256"],"frontend_patched_sha256":auth["patched_sha256"],
            "status":"preparing","private_state_retained_on_rollback":True}
    atomic_bytes(transaction/"transaction.json",json.dumps(record,indent=2).encode())
    try:
        target=install_release(args,manifest)
        install_private(args)
        point_current(target);pointer_changed=True
        for name in UNITS:
            template=target/"scripts/deploy"/(name+".example")
            content=template.read_bytes()
            if name=="jianweirs.service":content=content.replace(b"Environment=PYTHONUTF8=1",b"Environment=PYTHONUTF8=1\nEnvironment=WUYU_PROXY_IPS=172.20.0.2,172.29.0.3,127.0.0.1,172.17.0.1")
            atomic_bytes(SYSTEMD/name,content,0o644);written_units.append(name)
        command(["systemctl","daemon-reload"])
        command(["systemctl","restart",UNITS[0]])
        ready("http://127.0.0.1:8171/health")
        command(["systemctl","restart",UNITS[1]])
        ready("http://172.17.0.1:8170/health")
        session=ready("http://172.17.0.1:8170/portal/api/session").json()
        if session.get("auth",{}).get("mode")!="wuyu" or session.get("bootstrap_required") is not False:
            raise ActivationError("The prepared product did not enable central identity")
        frontend_changed=True
        apply_main_frontend(args,transaction)
        if NGINX.read_bytes()!=original:raise ActivationError("The shared nginx configuration changed during activation")
        atomic_bytes(NGINX,changed,0o644);nginx_changed=True
        command(["docker","exec","nginx-1.29","nginx","-t"])
        command(["docker","exec","nginx-1.29","nginx","-s","reload"])
        record["public_probes"]=public_probes()
        command(["systemctl","enable",*UNITS])
        record.update(status="activated",public_url=PUBLIC+"/jianweirs/",nginx_sha256=hashlib.sha256(changed).hexdigest())
    except BaseException as error:
        rollback=[]
        if nginx_changed:
            try:
                if NGINX.read_bytes()!=changed:raise ActivationError("Nginx was modified by another operator")
                atomic_bytes(NGINX,original,0o644)
                command(["docker","exec","nginx-1.29","nginx","-t"])
                command(["docker","exec","nginx-1.29","nginx","-s","reload"]);rollback.append("nginx_restored")
            except Exception:rollback.append("nginx_manual_review_required")
        if frontend_changed:
            try:
                if (transaction/"main.jsx.before").exists() and (transaction/"central-dist.before/index.html").exists():
                    auth=json.loads((args.auth_stage/"auth-return-verification.json").read_text())
                    if container_source_hash() not in {auth["original_sha256"],auth["patched_sha256"]}:
                        raise ActivationError("Central frontend was changed by another operator")
                    restore_main_frontend(transaction);rollback.append("main_frontend_restored")
                else:rollback.append("main_frontend_not_changed")
            except Exception:rollback.append("main_frontend_manual_review_required")
        for name in reversed(written_units):
            try:command(["systemctl","stop",name]);rollback.append(name+"_stopped")
            except Exception:rollback.append(name+"_stop_failed")
            backup=transaction/(name+".before")
            if backup.exists():atomic_bytes(SYSTEMD/name,backup.read_bytes(),0o644)
            else:
                command(["systemctl","disable",name],okay=(0,1));(SYSTEMD/name).unlink(missing_ok=True)
        if pointer_changed:
            if old_current:point_current(Path(old_current))
            elif (BASE/"current").is_symlink():(BASE/"current").unlink()
        command(["systemctl","daemon-reload"])
        for name,state in states.items():
            if state["active"]:command(["systemctl","start",name])
        record.update(status="rolled_back",failure=type(error).__name__,rollback=rollback)
        atomic_bytes(transaction/"transaction.json",json.dumps(record,indent=2).encode())
        raise ActivationError("Activation failed; transaction rollback recorded at "+str(transaction)) from None
    atomic_bytes(transaction/"transaction.json",json.dumps(record,indent=2).encode())
    return record|{"transaction":str(transaction)}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release")
    parser.add_argument("--archive",type=Path)
    parser.add_argument("--sha256")
    parser.add_argument("--private-state",type=Path)
    parser.add_argument("--runtime-release",type=Path,default=BASE/"releases/prep-20261025-b")
    parser.add_argument("--product-python-runtime",type=Path,
                        help="Isolated, audited Python runtime for a product-only dependency update")
    parser.add_argument('--backup-state',action='store_true',help='Back up private state while the product is stopped')
    parser.add_argument("--auth-stage",type=Path,default=BASE/"staging/auth-return-20261025-b")
    parser.add_argument("--activate",action="store_true")
    parser.add_argument("--product-update",action="store_true",
                        help="Update an existing product only; keep central frontend, nginx, science and private state unchanged")
    parser.add_argument("--rollback",type=Path)
    args=parser.parse_args()
    if args.product_python_runtime and not args.product_update:
        parser.error('A separate Python runtime requires --product-update')
    if args.backup_state and not args.product_update:
        parser.error('A stopped state backup requires --product-update')
    if args.rollback:
        if args.activate or args.product_update:parser.error("Choose activation/update or rollback")
        import fcntl
        with (BASE/".activation.lock").open("a+b") as lock:
            fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            result=rollback_transaction(args.rollback)
        print(json.dumps(result));return
    if not args.release or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{1,79}",args.release):parser.error("A safe release identifier is required")
    if not args.product_update and not args.private_state:parser.error("Initial activation requires private state")
    if args.activate and (not args.archive or not args.sha256):parser.error("Activation requires the final archive and its SHA256")
    os.umask(0o077)
    if args.product_update:
        import fcntl
        with (BASE/".activation.lock").open("a+b") as lock:
            fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            summary,manifest=product_update_preflight(args)
            result=product_update(args,manifest,summary) if args.activate else {'status':'product_update_preflight_passed_not_activated',**summary}
        print(json.dumps(result,ensure_ascii=True));return
    summary,manifest=preflight(args)
    if args.activate:
        import fcntl
        with (BASE/".activation.lock").open("a+b") as lock:
            fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            result=activate(args,manifest)
    else:result={"status":"preflight_passed_not_activated",**summary}
    print(json.dumps(result,ensure_ascii=True))


if __name__=="__main__":main()
