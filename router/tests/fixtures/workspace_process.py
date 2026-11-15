"""Synthetic workspace child: HTTP only, no tools/model/provider/database calls."""
import argparse,json,uuid
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer

p=argparse.ArgumentParser();p.add_argument('role');p.add_argument('port',type=int);p.add_argument('workspace');p.add_argument('--profile');args=p.parse_args()
class Handler(BaseHTTPRequestHandler):
    def log_message(self,*unused):pass
    def send(self,value,cookie=False):
        raw=json.dumps(value).encode();self.send_response(200)
        self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)))
        if cookie:self.send_header('Set-Cookie','fixture-grant=only; HttpOnly')
        self.end_headers();self.wfile.write(raw)
    def do_GET(self):self.send({'self_fixture':True},cookie=True)
    def do_POST(self):
        body=json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))))
        if self.path=='/api/workspace/create':
            assert body['payload']['args']['request']['path']==args.workspace
            return self.send({'result':{'ok':True,'value':{'workspace':{'workspaceId':'44444444-4444-4444-8444-444444444444','path':args.workspace}}}})
        if self.path=='/api/session/list':return self.send({'result':{'ok':True,'value':{'items':[]}}})
        self.send_error(405)
ThreadingHTTPServer(('127.0.0.1',args.port),Handler).serve_forever()
