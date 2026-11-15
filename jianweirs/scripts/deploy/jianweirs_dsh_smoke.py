"""Exercise the exact Linux headless planning profile with a local fake model."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time


class Model(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length",0)))
        chunks=[{"id":"local-smoke","object":"chat.completion.chunk","created":int(time.time()),"model":"smoke-model",
            "choices":[{"index":0,"delta":{"role":"assistant","content":"smoke-ready"},"finish_reason":None}]},
            {"id":"local-smoke","object":"chat.completion.chunk","created":int(time.time()),"model":"smoke-model",
            "choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":5,"completion_tokens":5,"total_tokens":10}}]
        body=("".join("data: "+json.dumps(chunk)+"\n\n" for chunk in chunks)+"data: [DONE]\n\n").encode()
        self.send_response(200);self.send_header("Content-Type","text/event-stream");self.send_header("Content-Length",str(len(body)));self.end_headers();self.wfile.write(body)
    def log_message(self,*args):pass


def main():
    root=Path(sys.argv[1]).resolve()
    stage=Path(sys.argv[2]).resolve()
    stage.mkdir(parents=True,exist_ok=True)
    http=ThreadingHTTPServer(("127.0.0.1",0),Model)
    thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
    environment={key:value for key,value in os.environ.items() if not re.search("API.?KEY|TOKEN|SECRET|PASSWORD|^WUYU_|^DEEPSEEK_|^METASO_",key,re.I)}
    environment.update(PATH="/opt/node/bin:"+environment.get("PATH",""),DSH_HOME=str(stage/"home"),
        WUYU_PLANNER_WORKDIR=str(stage),WUYU_MAIN_API_KEY="local-smoke-not-a-credential",
        WUYU_MAIN_BASE_URL=f"http://127.0.0.1:{http.server_port}/v1",WUYU_MAIN_MODEL="smoke-model",DSH_TELEMETRY_DISABLED="1")
    command=["/opt/node/bin/node","--import","tsx/esm","apps/cli/src/bin.ts","--profile","headless",
        "--patch",str(root/"harness-config/wuyu-main-compatible.cordis.yml"),"--patch",str(root/"harness-config/wuyu-planner.cordis.yml"),"--json","-"]
    try:
        result=subprocess.run(command,cwd=root/"deepseek-harness",env=environment,input=b"Return exactly smoke-ready",capture_output=True,timeout=60)
        (stage/"stdout.jsonl").write_bytes(result.stdout);(stage/"stderr.txt").write_bytes(result.stderr)
        final=[json.loads(line) for line in result.stdout.splitlines() if line.strip().startswith(b"{")]
        passed=result.returncode==0 and any(event.get("type")=="final" and "smoke-ready" in str(event.get("text")) for event in final)
        report={"status":"passed" if passed else "failed","exit_code":result.returncode,"events":[event.get("type") for event in final],"real_provider_called":False}
        (stage/"verification.json").write_text(json.dumps(report,indent=2));print(json.dumps(report))
        if not passed:raise SystemExit(1)
    finally:http.shutdown()


if __name__=="__main__":main()
