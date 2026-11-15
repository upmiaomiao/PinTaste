"""Local-only configuration helpers. Never prints generated API keys."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import secrets
from dotenv import dotenv_values
from .gateway import Cluster, ROOT


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['init', 'check'])
    parser.add_argument('--config', type=Path, default=ROOT/'cluster.json')
    parser.add_argument('--worker-config', type=Path, default=ROOT/'.worker.env')
    args = parser.parse_args()
    if args.command == 'init' and (not args.config.exists() or args.config.stat().st_size == 0):
        worker = dotenv_values(args.worker_config)
        key = worker.get('OMAP_WORKER_API_KEY')
        if not key or len(key) < 32:
            raise SystemExit('Run scripts/deploy/setup-worker.ps1 first; a local worker key is required.')
        values = json.loads((ROOT/'cluster.example.json').read_text(encoding='utf-8-sig'))
        values['nodes'][0]['api_key'] = key
        values['clients'][0]['api_key'] = secrets.token_urlsafe(32)
        # Disabled example nodes remain placeholders until another machine is
        # configured. The first node is the real local authenticated worker.
        args.config.write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding='utf-8')
    try:
        config = Cluster.model_validate(json.loads(args.config.read_text(encoding='utf-8-sig')))
        if any(c.api_key.startswith('REPLACE_') for c in config.clients) or any(n.enabled and n.api_key.startswith('REPLACE_') for n in config.nodes):
            raise ValueError('placeholder')
    except (OSError, ValueError):
        raise SystemExit('Private cluster configuration is missing or invalid; no credential values were printed.') from None
    print(json.dumps({'config_path': str(args.config), 'enabled_nodes': [n.id for n in config.nodes if n.enabled],
                      'clients': [c.id for c in config.clients], 'keys_printed': False}, ensure_ascii=False))


if __name__ == '__main__':
    main()
