#!/usr/bin/env bash
# Prepare only. This script never starts a service or changes the shared proxy.
set -euo pipefail
umask 077
archive=${1:?source archive required}
release=${2:?release identifier required}
base=/opt/wuyu-jianweirs
[[ "$release" =~ ^[a-z0-9][a-z0-9_.-]{1,79}$ ]] || exit 2
destination="$base/releases/$release"
[[ ! -e "$destination" ]] || { echo 'Release already exists; choose a fresh release identifier.'; exit 2; }
mkdir -p "$base/releases" "$base/shared" "$base/staging" "$destination"
python3 - "$archive" "$destination" <<'PY'
import hashlib,json,pathlib,sys,tarfile
archive,destination=pathlib.Path(sys.argv[1]),pathlib.Path(sys.argv[2])
with tarfile.open(archive) as source:
    source.extractall(destination,filter='data')
manifest=json.loads((destination/'release-manifest.json').read_text())
for name,digest in manifest['files'].items():
    target=(destination/name).resolve()
    if not target.is_relative_to(destination.resolve()) or hashlib.sha256(target.read_bytes()).hexdigest()!=digest:
        raise SystemExit('Source manifest validation failed')
print(json.dumps({'source_manifest':'passed','files':len(manifest['files'])}))
PY
python3 -m venv "$destination/.venv"
"$destination/.venv/bin/python" -m pip install --disable-pip-version-check -r "$destination/scripts/deploy/jianweirs_requirements.lock"
"$destination/.venv/bin/python" -c 'import fastapi,httpx,rasterio,cv2,docx,cryptography; print("Python dependencies passed")'
export PATH=/opt/node/bin:$PATH
export COREPACK_ENABLE_DOWNLOAD_PROMPT=0
export DSH_TELEMETRY_DISABLED=1
export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
export ELECTRON_SKIP_BINARY_DOWNLOAD=1
cd "$destination/deepseek-harness"
corepack pnpm install --filter @deepseek-ai/dsh... --ignore-scripts --frozen-lockfile
node --import tsx/esm native/system/scripts/build.ts --host-addon-only
node --import tsx/esm apps/cli/src/bin.ts --help > "$destination/dsh-help.txt"
"$destination/.venv/bin/python" -m pip freeze > "$destination/python-installed.txt"
printf '%s\n' "Prepared: $destination" 'No service started and no nginx configuration changed.'
