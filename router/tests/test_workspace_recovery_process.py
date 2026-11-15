"""Real self-created subprocesses in a separate network namespace, no user jobs."""
import asyncio,json,os,pathlib,socket,sys,tempfile,threading,unittest
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from aiohttp import ClientSession
from test_workspace_idle import router,USER,LEGACY


class IdleHandler(BaseHTTPRequestHandler):
    def log_message(self,*unused):pass
    def do_GET(self):
        assert self.path=='/health/idle'
        raw=b'{"schema_version":1,"idle":true}'
        self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)


class ProcessRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.assertNotEqual(os.readlink('/proc/self/ns/net'),os.environ['FIXTURE_HOST_NET_NAMESPACE'],'Must run in an isolated network namespace')
        self.temp=tempfile.TemporaryDirectory(prefix='wuyu-router143-');self.addCleanup(self.temp.cleanup)
        root=pathlib.Path(self.temp.name);seed=root/'seed';seed.mkdir();(seed/'retained.txt').write_text('self-created retained bytes')
        self.guard=ThreadingHTTPServer(('127.0.0.1',18131),IdleHandler)
        self.thread=threading.Thread(target=self.guard.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.guard.server_close);self.addCleanup(self.guard.shutdown)
        # Reserve only a fresh pair in the isolated namespace, never live ports.
        for _ in range(100):
            with socket.socket()as first:
                first.bind(('127.0.0.1',0));port=first.getsockname()[1]
                if port>=65534:continue
                try:
                    with socket.socket()as second:second.bind(('127.0.0.1',port+1));break
                except OSError:continue
        else:raise RuntimeError('No fixture port pair')
        script=str(pathlib.Path(__file__).parent/'fixtures/workspace_process.py')
        command=lambda role,port:[sys.executable,'-B',script,role,port,'@@WORKSPACE@@','--profile','web']
        config={'testing':True,'tenant_root':str(root/'tenants'),'seed_dir':str(seed),'legacy_user_id':LEGACY,'max_tenants':1,'port_start':port,'public_origin':'http://fixture.invalid','idle_seconds':900,'idle_check_seconds':30,'startup_timeout':10,'idle_guard_backend':'http','idle_probe_url':'http://127.0.0.1:18131/health/idle','agent_argv':command('agent','@@AGENT_PORT@@'),'host_argv':command('host','@@HOST_PORT@@'),'agent_environment':{},'host_environment':{}}
        self.manager=router.TenantManager(config);self.addAsyncCleanup(self.manager.close)
        self.client=ClientSession();self.addAsyncCleanup(self.client.close);self.manager.idle_client=self.client

    async def test_real_dead_agent_retires_then_reopens_original_home_without_jobs(self):
        tenant=await self.manager.resolve({'id':USER},'fixture-actor-token')
        original=tuple(p.pid for p in tenant.processes);marker=self.manager.root/USER/'home/retained.txt'
        self.assertEqual(marker.read_text(),'self-created retained bytes')
        tenant.processes[0].terminate();await tenant.processes[0].wait();tenant.last_activity=0
        await self.manager.reap_idle(self.client)
        self.assertNotIn(USER,self.manager.tenants);self.assertIsNotNone(tenant.processes[1].returncode)
        self.assertEqual(self.manager.cached_history({'id':USER}),[]);self.assertEqual(marker.read_text(),'self-created retained bytes')
        replacement=await self.manager.resolve({'id':USER},'fixture-new-actor-token')
        self.assertNotEqual(tuple(p.pid for p in replacement.processes),original)
        self.assertTrue(all(p.returncode is None for p in replacement.processes));self.assertEqual(marker.read_text(),'self-created retained bytes')
        self.assertEqual(self.manager.idle_counters['recovered'],1);self.assertEqual(self.manager.idle_counters['woken'],1)
        self.assertEqual(len(self.manager.tenants),1)

    async def test_real_dead_host_does_not_stop_live_agent_or_guess_native_idle(self):
        tenant=await self.manager.resolve({'id':USER},'fixture-actor-token')
        tenant.processes[1].terminate();await tenant.processes[1].wait();tenant.last_activity=0
        await self.manager.reap_idle(self.client)
        self.assertIs(self.manager.tenants[USER],tenant);self.assertIsNone(tenant.processes[0].returncode)
        with self.assertRaises(router.web.HTTPServiceUnavailable):await self.manager.resolve({'id':USER},'fixture-new-actor-token')


if __name__=='__main__':unittest.main()
