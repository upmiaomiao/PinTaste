import json, sys
from pathlib import Path
sys.path.insert(0, '/opt/wuyu-staging/source/router')
from dmx_model_gateway import create_app
from aiohttp import web
c = json.loads(Path('/opt/wuyu-staging/private/gateway.json').read_text())
p = json.loads(Path(c['credentials_path']).read_text())
key = Path(c['internal_key_path']).read_text().strip()
app = create_app([p['primary'], p['standby']], key, queue_timeout=c['queue_timeout'], request_timeout=c['request_timeout'], max_body=c['max_body'], per_model_capacity=200, per_model_queue=256, web_capacity=16, web_queue=128, body_budget_bytes=512 * 1024 * 1024, body_memory_factor=4, body_queue_limit=2048, body_read_timeout=60)
web.run_app(app, host=c['host'], port=c['port'], access_log=None, print=None, handler_cancellation=True)
