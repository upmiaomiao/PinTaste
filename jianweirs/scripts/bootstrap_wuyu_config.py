"""Privately copy existing local provider credentials into this project's env.

Old platform databases are opened read-only. Existing destination variables,
including deliberately blank ones, are preserved. No credential is printed or
placed in a subprocess argument. Run with the same Windows account that owns
the RS deployment's DPAPI credential.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import uuid

from dotenv import dotenv_values


PROJECT = Path(__file__).resolve().parents[1]


def _settings(folder: Path) -> dict:
    connection = sqlite3.connect((folder / "platform.sqlite3").as_uri() + "?mode=ro", uri=True)
    try:
        values = dict(connection.execute("SELECT key,value FROM settings"))
    finally:
        connection.close()
    result = {}
    for key, value in values.items():
        try:
            result[key] = json.loads(value)
        except (ValueError, TypeError):
            result[key] = value
    return result


def _fernet(folder: Path, encrypted: str) -> str:
    from cryptography.fernet import Fernet
    if not encrypted:
        raise ValueError("A required saved platform credential is missing.")
    return Fernet((folder / "vault.key").read_bytes()).decrypt(encrypted.encode("ascii")).decode("utf-8")


def _dpapi_key(path: Path) -> str:
    if os.name != "nt":
        raise ValueError("RS DPAPI credential import requires its owning Windows account.")
    command = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$credential = Import-Clixml -LiteralPath $env:WUYU_BOOTSTRAP_DPAPI_PATH
$pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($credential)
try { [Console]::Out.Write([Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)) }
finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
"""
    environment = dict(os.environ, WUYU_BOOTSTRAP_DPAPI_PATH=str(path))
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", command],
        capture_output=True, env=environment, timeout=20,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        raise ValueError("Windows could not unlock the saved RS credential for this account.")
    value = result.stdout.decode("utf-8").strip()
    if not value:
        raise ValueError("The saved RS credential is empty.")
    return value


def _restrict_file(path: Path) -> None:
    if os.name != "nt":
        path.chmod(0o600)
        return
    identity = subprocess.run(
        ["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=10,
    )
    if identity.returncode:
        raise ValueError("Could not identify the file owner for a private credential file.")
    rows = list(csv.reader(identity.stdout.decode("utf-8", errors="replace").splitlines()))
    sid = rows[-1][-1] if rows else ""
    if not sid.startswith("S-1-"):
        raise ValueError("Could not resolve the Windows file-owner SID.")
    secured = subprocess.run(
        ["icacls.exe", str(path), "/inheritance:r", "/grant:r", "*" + sid + ":(F)"],
        capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=10,
    )
    if secured.returncode:
        raise ValueError("Could not restrict the destination credential file to its owner.")


def _main_provider(platform_root: Path) -> dict:
    runtime = platform_root / "runtime"
    connection = sqlite3.connect((runtime / "platform.sqlite3").as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute("SELECT * FROM models WHERE id=? AND enabled=1", ("deepseek-brain",)).fetchone()
    finally:
        connection.close()
    if row is None or row["model"] != "deepseek-v4.1-flash":
        raise ValueError("The saved DeepSeek V4.1 Flash provider was not found.")
    return {
        "WUYU_MAIN_BASE_URL": row["base_url"].rstrip("/"),
        "WUYU_MAIN_MODEL": row["model"],
        "WUYU_MAIN_API_KEY": _fernet(runtime, row["secret"]),
    }


def collect(platform_root: Path, rs_root: Path, worker_env: Path) -> dict[str, str]:
    official_runtime = platform_root / "official" / "runtime"
    official = _settings(official_runtime)
    if official.get("display_name") != "wuyu-shensi-base":
        raise ValueError("The saved primary model is not the expected Wuyu Shensi deployment.")
    rs = json.loads((rs_root / "connection.json").read_text(encoding="utf-8-sig"))
    if rs.get("model") != "wuyu-jianwei-rs":
        raise ValueError("The saved RS deployment does not advertise wuyu-jianwei-rs.")
    worker = dotenv_values(worker_env, encoding="utf-8-sig")
    values = {
        **_main_provider(platform_root),
        "WUYU_SHENSI_BASE_URL": official["base_url"].rstrip("/"),
        "WUYU_SHENSI_MODEL": official["model"],
        "WUYU_SHENSI_API_KEY": _fernet(official_runtime, official["model_secret"]),
        "WUYU_SHENSI_TRUST_ENV": "0",
        "WUYU_RS_BASE_URL": rs["base_url"].rstrip("/"),
        "WUYU_RS_MODEL": rs["model"],
        "WUYU_RS_OUTPUT_FORMAT": "xml",
        "WUYU_RS_TRUST_ENV": "0",
        "WUYU_RS_API_KEY": _dpapi_key(rs_root / "api-key.dpapi.xml"),
        "METASO_API_KEY": _fernet(official_runtime, official["search_secret"]),
        "METASO_BASE_URL": "https://metaso.cn",
        "WUYU_DATA_BASE_URL": "http://127.0.0.1:8765",
        "WUYU_DATA_MODE": "auto",
        "WUYU_DATA_API_KEY": worker.get("OMAP_WORKER_API_KEY") or "",
    }
    if any(not isinstance(value, str) or "\n" in value or "\r" in value for value in values.values()):
        raise ValueError("A saved provider field contains unsupported multiline data.")
    return values


def write_missing(destination: Path, values: dict[str, str]) -> dict:
    previous = destination.read_text(encoding="utf-8-sig") if destination.exists() else ""
    existing = dotenv_values(destination, encoding="utf-8-sig") if destination.exists() else {}
    added = {name: value for name, value in values.items() if name not in existing}
    if added:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name("." + destination.name + "." + uuid.uuid4().hex + ".tmp")
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            _restrict_file(temporary)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
                descriptor = None
                output.write(previous)
                if previous and not previous.endswith("\n"):
                    output.write("\n")
                output.write("\n# Privately imported from existing local deployments; existing values preserved.\n")
                for name, value in added.items():
                    output.write(name + "=" + json.dumps(value, ensure_ascii=False) + "\n")
            temporary.replace(destination)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)
    current = dotenv_values(destination, encoding="utf-8-sig")
    return {
        "destination": str(destination), "added_names": list(added),
        "preserved_names": [name for name in values if name in existing],
        "configured": {name: bool(current.get(name)) for name in values},
        "credential_values_disclosed": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform-root", type=Path, default=Path(r"D:\无隅公司资料\wuyu-ai-platform"))
    parser.add_argument("--rs-root", type=Path, default=Path(r"D:\无隅公司资料\remote-sensing-deploy-20261012"))
    parser.add_argument("--worker-env", type=Path, default=PROJECT / ".worker.env")
    parser.add_argument("--output", type=Path, default=PROJECT / ".env.wuyu")
    args = parser.parse_args()
    try:
        values = collect(args.platform_root.resolve(), args.rs_root.resolve(), args.worker_env.resolve())
        report = write_missing(args.output.resolve(), values)
        report["source_paths"] = [
            str(args.platform_root / "runtime" / "platform.sqlite3"),
            str(args.platform_root / "official" / "runtime" / "platform.sqlite3"),
            str(args.rs_root / "connection.json"), str(args.rs_root / "api-key.dpapi.xml"),
            str(args.worker_env),
        ]
        # ASCII transport also works from a legacy Windows terminal.
        print(json.dumps(report, ensure_ascii=True, indent=2))
        return 0
    except Exception as error:
        # Never include exception messages that could echo decrypted values.
        print(json.dumps({"status": "import_failed", "error_type": type(error).__name__, "credential_values_disclosed": False}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
