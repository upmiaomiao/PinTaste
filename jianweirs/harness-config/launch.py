"""Launch the supported upstream dsh CLI with local Wuyu overlays and credentials."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlsplit

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / "deepseek-harness"
OFFICIAL_MAIN_BASE = "https://api.deepseek.com/v1"


def official_main_origin(value: str) -> bool:
    """Only the official HTTPS origin may borrow DEEPSEEK_API_KEY."""
    try:
        parts = urlsplit(value)
        return (parts.scheme == "https" and parts.hostname == "api.deepseek.com"
                and parts.port in (None,443) and not parts.username and not parts.password
                and not parts.query and not parts.fragment and parts.path.rstrip('/') in {'','/v1'})
    except ValueError:
        return False


def prepare_main_environment(environment: dict[str,str], *, force_compatible: bool = False) -> tuple[dict[str,str],bool,list[str]]:
    """Prepare child-only routing; never persist or return credentials as status.

    Official Flash uses the existing OpenAI-compatible adapter and declares its
    image capability. Unrelated endpoints never receive an official key fallback.
    An explicitly configured modality list remains authoritative.
    """
    child = dict(environment)
    base = child.get('WUYU_MAIN_BASE_URL','').strip()
    key = child.get('WUYU_MAIN_API_KEY','')
    if base:
        child['WUYU_MAIN_BASE_URL'] = base
    if not base and not key and child.get('DEEPSEEK_API_KEY'):
        base = OFFICIAL_MAIN_BASE
        child['WUYU_MAIN_BASE_URL'] = base
        child['WUYU_MAIN_API_KEY'] = child['DEEPSEEK_API_KEY']
        child.setdefault('WUYU_MAIN_MODEL','deepseek-flash')
    elif base and official_main_origin(base) and not key and child.get('DEEPSEEK_API_KEY'):
        child['WUYU_MAIN_API_KEY'] = child['DEEPSEEK_API_KEY']
    compatible = force_compatible or bool(base or child.get('WUYU_MAIN_API_KEY'))
    official_flash = official_main_origin(base) and child.get('WUYU_MAIN_MODEL','deepseek-flash') == 'deepseek-flash'
    default_input = 'text,image' if official_flash else 'text'
    raw_input = child.get('WUYU_MAIN_INPUT',default_input)
    modalities = list(dict.fromkeys(part.strip() for part in raw_input.split(',') if part.strip()))
    if compatible and (not modalities or 'text' not in modalities or any(part not in {'text','image'} for part in modalities)):
        raise ValueError('WUYU_MAIN_INPUT must include text and may also declare image')
    if compatible:
        child['WUYU_MAIN_INPUT'] = ','.join(modalities)
    return child,compatible,modalities


def main() -> int:
    """Prepare a child environment, validate prerequisites, and call the official CLI."""
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", required=True, help="Supported Node executable")
    parser.add_argument("--profile", choices=("web", "headless"), default="web")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--dump-config", action="store_true")
    parser.add_argument("--custom-main-model", action="store_true")
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3082)
    parser.add_argument("--task")
    options = parser.parse_args()

    load_dotenv(ROOT / ".env.wuyu", override=False)
    child_env = dict(os.environ)
    child_env["WUYU_PROJECT_ROOT"] = str(ROOT)
    child_env["WUYU_PYTHON"] = sys.executable
    child_env["PYTHONUTF8"] = "1"
    child_env["PYTHONIOENCODING"] = "utf-8"
    child_env.setdefault("DSH_HOME", str(ROOT / "outputs" / "harness-home"))
    child_env["PATH"] = str(Path(options.node).resolve().parent) + os.pathsep + child_env.get("PATH", "")
    try:
        child_env,custom_main,main_input = prepare_main_environment(child_env,force_compatible=options.custom_main_model)
    except ValueError as exc:
        parser.error(str(exc))

    version = subprocess.run(
        [options.node, "--version"], capture_output=True, text=True, check=True, env=child_env
    ).stdout.strip()
    parts = tuple(int(part) for part in version.lstrip("v").split(".")[:3])
    if not (parts >= (24, 0, 0) or (22, 19, 0) <= parts < (23, 0, 0)):
        parser.error(f"Node {version} is unsupported; upstream requires ^22.19.0 or >=24.0.0")

    source_ready = (HARNESS / "apps" / "cli" / "src" / "bin.ts").is_file()
    dependencies_ready = (HARNESS / "node_modules" / "tsx" / "package.json").is_file()
    build_ready = (HARNESS / "packages" / "core" / "agent-loop" / "lib" / "index.js").is_file()
    backend_ready = (ROOT / "wuyu_harness" / "mcp.py").is_file()
    if options.check:
        print(json.dumps({
            "node_version": version,
            "upstream_checkout": source_ready,
            "upstream_dependencies": dependencies_ready,
            "upstream_build": build_ready,
            "wuyu_mcp_module": backend_ready,
            "private_env_file": (ROOT / ".env.wuyu").is_file(),
            "main_provider": "wuyu-main" if custom_main else "deepseek-official",
            "main_input_modalities": main_input if custom_main else "provider_catalog",
            "main_model": child_env.get('WUYU_MAIN_MODEL','deepseek-flash'),
            "main_api": "openai-completions" if custom_main else "provider_catalog",
            "main_credential_configured": bool(child_env.get(
                "WUYU_MAIN_API_KEY" if custom_main else "DEEPSEEK_API_KEY"
            )),
            "harness_home": child_env["DSH_HOME"],
        }, ensure_ascii=False, indent=2))
        return 0 if all((source_ready, dependencies_ready, build_ready, backend_ready)) else 1

    if not source_ready:
        parser.error("The upstream clone is incomplete: deepseek-harness/apps/cli/src/bin.ts is missing")
    if not dependencies_ready or not build_ready:
        parser.error("Upstream dependencies/build are missing. Run start-wuyu-harness.ps1 -Prepare first")
    if not backend_ready and not options.dump_config:
        parser.error("The Wuyu MCP backend is missing: wuyu_harness/mcp.py")
    if custom_main and not options.dump_config:
        if not child_env.get("WUYU_MAIN_BASE_URL") or not child_env.get("WUYU_MAIN_API_KEY"):
            parser.error("Custom main model requires WUYU_MAIN_BASE_URL and WUYU_MAIN_API_KEY in .env.wuyu")
    if options.profile == "headless" and not options.task and not options.dump_config:
        parser.error("--profile headless requires --task")
    if not 1 <= options.port <= 65535:
        parser.error("--port must be between 1 and 65535")

    # Same source launcher used by upstream's `pnpm dsh`; no alternate app runtime.
    command = [options.node, "--import", "tsx/esm", "apps/cli/src/bin.ts",
               "--profile", options.profile,
               "--patch", str(ROOT / "harness-config" / "wuyu.cordis.yml")]
    if options.profile == "web":
        command += ["--patch", str(ROOT / "harness-config" / "wuyu-web.cordis.yml")]
    if custom_main:
        command += ["--patch", str(ROOT / "harness-config" / "wuyu-main-compatible.cordis.yml")]
    if options.dump_config:
        command += ["--dump-config"]
    elif options.profile == "web":
        command += ["--host", options.host, "--port", str(options.port)]
        if options.no_open:
            command += ["--no-open"]
    else:
        command.append(options.task)
    try:
        return subprocess.run(command, cwd=HARNESS, env=child_env, check=False).returncode
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
