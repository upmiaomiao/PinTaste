"""Real local aiohttp only: fake upstream, synthetic keys and small fixtures."""
import asyncio
from collections import Counter
import importlib.util
import json
from pathlib import Path
import unittest

from aiohttp import ClientSession, ClientTimeout, TCPConnector, web

R=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('candidate_gateway',R/'dmx_model_gateway.py')
gateway=importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway)
KEY='self_created_fixture_internal_key_'*2
KEYS=['self_created_fixture_primary','self_created_fixture_standby']
A='gemini-4-argon'
B='gemini-4-argon-ssvip'
C='gpt-image-2.5-sunburst-ssvip'


class GatewayCapacityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)
        self.runners=[]
        self.session=ClientSession(connector=TCPConnector(limit=0,limit_per_host=0),timeout=ClientTimeout(total=60))
        self.tasks=[]
        self.releases=[]

    async def asyncTearDown(self):
        for event in self.releases:event.set()
        for task in self.tasks:
            if not task.done():task.cancel()
        if self.tasks:await asyncio.gather(*self.tasks,return_exceptions=True)
        await self.session.close()
        for runner in reversed(self.runners):await runner.cleanup()

    async def start(self,handler,**options):
        upstream=web.Application(client_max_size=110*1024*1024)
        upstream.router.add_route('*','/{path:.*}',handler)
        ur=web.AppRunner(upstream,access_log=None,handler_cancellation=True)
        await ur.setup();site=web.TCPSite(ur,'127.0.0.1',0,backlog=2048);await site.start()
        self.runners.append(ur)
        port=site._server.sockets[0].getsockname()[1]
        self.app=gateway.create_app(KEYS,KEY,upstream='http://127.0.0.1:'+str(port),testing=True,**options)
        runner=web.AppRunner(self.app,access_log=None,handler_cancellation=True)
        await runner.setup();ps=web.TCPSite(runner,'127.0.0.1',0,backlog=2048);await ps.start()
        self.runners.append(runner)
        self.url='http://127.0.0.1:'+str(ps._server.sockets[0].getsockname()[1])

    def event(self):
        value=asyncio.Event();self.releases.append(value);return value

    def spawn(self,coro):
        value=asyncio.create_task(coro);self.tasks.append(value);return value

    async def post(self,model=A,path='/v1/chat/completions',**kwargs):
        if 'data'not in kwargs and 'json'not in kwargs:kwargs['json']={'model':model,'n':1,'messages':[]}
        async with self.session.post(self.url+path,headers={'Authorization':'Bearer '+KEY,**kwargs.pop('headers',{})},**kwargs)as response:
            return response.status,await response.read()

    async def health(self):
        async with self.session.get(self.url+'/health',headers={'Authorization':'Bearer '+KEY})as response:
            self.assertEqual(response.status,200);return await response.json()

    async def until(self,predicate,timeout=30):
        async with asyncio.timeout(timeout):
            while True:
                value=predicate()
                if hasattr(value,'__await__'):value=await value
                if value:return value
                await asyncio.sleep(.005)

    async def test_two_models_each_200_real_requests_and_no_writing_8_cap(self):
        release=self.event();active=Counter();peak=Counter();started=Counter();keys=Counter()
        async def handler(request):
            data=await request.json();model=data['model'];active[model]+=1;started[model]+=1
            peak[model]=max(peak[model],active[model]);keys[request.headers.get('Authorization')]+=1
            try:await release.wait();return web.json_response({'ok':True})
            finally:active[model]-=1
        await self.start(handler)
        tasks=[self.spawn(self.post(model))for model in (A,B)for _ in range(200)]
        await self.until(lambda:active[A]==active[B]==200)
        state=await self.health()
        self.assertEqual(state['active'],[200,200])
        self.assertEqual(state['per_model'][A]['active_total'],200)
        self.assertEqual(state['per_model'][B]['active_total'],200)
        self.assertEqual(state['writing_active'],400)
        self.assertEqual(self.app['client'].connector.limit,0)
        waiter=self.spawn(self.post(A))
        await self.until(lambda:len(self.app['pool']._buckets[A]['queue'])==1)
        other=self.spawn(self.post(C))
        await self.until(lambda:active[C]==1)
        self.assertEqual(started[A],200)
        self.assertEqual(peak[A],200);self.assertEqual(peak[B],200)
        release.set()
        results=await asyncio.gather(*tasks,waiter,other)
        self.assertTrue(all(status==200 for status,_ in results))
        self.assertEqual(started[A],201)
        self.assertEqual(len(keys),2)
        state=await self.health()
        self.assertEqual(sum(state['active']),0)
        self.assertEqual(state['queued'],0)
        self.assertEqual(state['body_budget']['reserved_bytes'],0)

    async def test_201st_one_model_waits_other_model_starts(self):
        release=self.event();started=Counter()
        async def handler(request):
            model=(await request.json())['model'];started[model]+=1
            await release.wait();return web.json_response({'ok':True})
        await self.start(handler)
        tasks=[self.spawn(self.post(A))for _ in range(200)]
        await self.until(lambda:started[A]==200)
        waiter=self.spawn(self.post(A))
        await self.until(lambda:len(self.app['pool']._buckets[A]['queue'])==1)
        other=self.spawn(self.post(B))
        await self.until(lambda:started[B]==1)
        self.assertEqual(started[A],200)
        release.set();results=await asyncio.gather(*tasks,waiter,other)
        self.assertTrue(all(s==200 for s,_ in results))

    async def test_model_queue_timeout_has_distinct_code_and_cleans_permits(self):
        release=self.event();started=Counter()
        async def handler(request):
            started[(await request.json())['model']]+=1
            await release.wait();return web.json_response({'ok':True})
        await self.start(handler,per_model_capacity=1,queue_timeout=.08)
        holder=self.spawn(self.post())
        await self.until(lambda:started[A]==1)
        status,body=await self.post()
        self.assertEqual(status,504)
        error=json.loads(body)['error']
        self.assertEqual(error['code'],'MODEL_QUEUE_TIMEOUT')
        self.assertEqual(error['stage'],'model_queue')
        self.assertEqual(started[A],1)
        self.assertEqual((await self.health())['queued'],0)
        release.set();self.assertEqual((await holder)[0],200)
        self.assertEqual((await self.health())['body_budget']['reserved_bytes'],0)

    async def test_model_queue_full_is_local_429(self):
        release=self.event();entered=self.event()
        async def handler(request):
            await request.read();entered.set();await release.wait();return web.json_response({'ok':True})
        await self.start(handler,per_model_capacity=1,per_model_queue=1)
        holder=self.spawn(self.post());await entered.wait()
        waiter=self.spawn(self.post());await self.until(lambda:len(self.app['pool']._buckets[A]['queue'])==1)
        status,body=await self.post()
        self.assertEqual(status,429);self.assertEqual(json.loads(body)['error']['code'],'MODEL_QUEUE_FULL')
        release.set();await asyncio.gather(holder,waiter)

    async def test_http_cancel_queued_request_releases_queue_and_body(self):
        release=self.event();entered=self.event()
        async def handler(request):
            await request.read();entered.set();await release.wait();return web.json_response({'ok':True})
        await self.start(handler,per_model_capacity=1)
        holder=self.spawn(self.post());await entered.wait()
        waiter=self.spawn(self.post());await self.until(lambda:len(self.app['pool']._buckets[A]['queue'])==1)
        waiter.cancel();await asyncio.gather(waiter,return_exceptions=True)
        await self.until(lambda:not self.app['pool']._buckets[A]['queue'])
        self.assertEqual(self.app['body_budget'].active,1)
        release.set();await holder
        self.assertEqual(self.app['body_budget'].used,0)

    async def test_upstream_timeout_is_not_queue_timeout(self):
        release=self.event()
        async def handler(request):
            await request.read();await release.wait();return web.json_response({'ok':True})
        await self.start(handler,request_timeout=.08)
        status,body=await self.post()
        self.assertEqual(status,504)
        error=json.loads(body)['error']
        self.assertEqual(error['code'],'MODEL_UPSTREAM_TIMEOUT');self.assertEqual(error['stage'],'upstream')
        self.assertGreaterEqual(error['upstream_seconds'],.07)
        state=await self.health();self.assertEqual(sum(state['active']),0);self.assertEqual(state['body_budget']['reserved_bytes'],0)

    async def test_read_before_memory_budget_is_prevented_and_wait_timeout_safe(self):
        release=self.event();entered=self.event();reads=[]
        original=gateway.bounded_request_body
        async def observed(*args):reads.append(1);return await original(*args)
        gateway.bounded_request_body=observed
        self.addCleanup(setattr,gateway,'bounded_request_body',original)
        async def handler(request):
            await request.read();entered.set();await release.wait();return web.json_response({'ok':True})
        maximum=1024*1024
        await self.start(handler,max_body=maximum,body_budget_bytes=4*maximum,queue_timeout=.1)
        raw=json.dumps({'model':A,'n':1,'prompt':'x'*(maximum-200)}).encode()
        # Unknown/chunked length reserves the full per-request maximum.
        async def chunked():yield raw
        holder=self.spawn(self.post(data=chunked(),headers={'Content-Type':'application/json'}));await entered.wait()
        status,body=await self.post(data=chunked(),headers={'Content-Type':'application/json'})
        self.assertEqual(status,504);self.assertEqual(json.loads(body)['error']['stage'],'body_bytes')
        self.assertEqual(len(reads),1)
        state=await self.health();self.assertEqual(state['body_budget']['active_bodies'],1)
        self.assertLessEqual(state['body_budget']['peak_reserved_bytes'],4*maximum)
        self.assertEqual(state['body_budget']['waiting_requests'],0)
        release.set();await holder
        self.assertEqual(self.app['body_budget'].used,0)

    async def test_body_wait_cancel_frees_ticket_then_next_request_can_run(self):
        budget=gateway.BodyBudget(4*1024*1024)
        async with budget.reserve(1024*1024,1):
            async def waiting():
                async with budget.reserve(1024*1024,3):pass
            task=self.spawn(waiting());await self.until(lambda:len(budget._queue)==1)
            task.cancel();await asyncio.gather(task,return_exceptions=True)
            self.assertEqual(len(budget._queue),0)
        async with budget.reserve(1024*1024,1):self.assertEqual(budget.active,1)
        self.assertEqual(budget.used,0)

    async def test_declared_and_chunked_oversize_do_not_leak_budget_or_forward(self):
        calls=[]
        async def handler(request):calls.append(1);return web.json_response({'ok':True})
        maximum=1024*1024
        await self.start(handler,max_body=maximum,body_budget_bytes=4*maximum)
        status,_=await self.post(data=b'x'*(maximum+1),headers={'Content-Type':'application/json'})
        self.assertEqual(status,413)
        async def chunked():yield b'x'*(maximum+1)
        status,_=await self.post(data=chunked(),headers={'Content-Type':'application/json'})
        self.assertEqual(status,413);self.assertEqual(calls,[])
        self.assertEqual(self.app['body_budget'].used,0)

    async def test_web_tools_have_independent_bounded_bucket(self):
        release=self.event();started=Counter()
        async def handler(request):
            data=await request.json();started[request.path]+=1
            await release.wait();return web.json_response({'ok':True})
        await self.start(handler,per_model_capacity=1,web_capacity=1,web_queue=1)
        model=self.spawn(self.post());await self.until(lambda:started['/v1/chat/completions']==1)
        tool=self.spawn(self.post(path='/v1/tools/search',json={'query':'self created fixture'}))
        await self.until(lambda:started['/v1/tools/search']==1)
        waiter=self.spawn(self.post(path='/v1/tools/search',json={'query':'self created fixture'}))
        await self.until(lambda:len(self.app['pool']._buckets[gateway.ModelPool.WEB_BUCKET]['queue'])==1)
        status,body=await self.post(path='/v1/tools/search',json={'query':'self created fixture'})
        self.assertEqual(status,429);self.assertEqual(json.loads(body)['error']['code'],'MODEL_QUEUE_FULL')
        state=await self.health();self.assertEqual(state['web_tools']['active_total'],1);self.assertEqual(state['per_model'][A]['active_total'],1)
        release.set();await asyncio.gather(model,tool,waiter)

    async def test_validation_auth_allowlist_n_and_success_forwarding_preserved(self):
        seen=[]
        async def handler(request):seen.append(await request.read());return web.json_response({'ok':True})
        await self.start(handler)
        async with self.session.post(self.url+'/v1/chat/completions',json={'model':A})as response:self.assertEqual(response.status,401)
        for data in [{'model':'not-enabled'}, {'model':A,'n':2}, ['not-an-object']]:
            self.assertEqual((await self.post(json=data))[0],400)
        raw=json.dumps({'model':C,'n':1,'prompt':'self created fixture'}).encode()
        self.assertEqual((await self.post(data=raw,headers={'Content-Type':'application/json'}))[0],200)
        self.assertEqual(seen,[raw])

    async def test_configuration_is_bounded(self):
        for kwargs in [{'per_model_capacity':201},{'per_model_capacity':0},{'body_memory_factor':1},{'body_budget_bytes':4*1024*1024},{'web_capacity':201}]:
            with self.assertRaises(ValueError):gateway.create_app(KEYS,KEY,**kwargs)

    async def test_real_100MiB_bodies_are_byte_limited_before_read_and_recover(self):
        maximum=100*1024*1024
        prefix=b'{"model":"'+A.encode()+b'","n":1,"prompt":"'
        raw=prefix+b'x'*(maximum-len(prefix)-2)+b'"}'
        self.assertEqual(len(raw),maximum)
        release=self.event();entered=self.event();lengths=[];reads=[]
        original=gateway.bounded_request_body
        async def observed(*args):reads.append(1);return await original(*args)
        gateway.bounded_request_body=observed
        self.addCleanup(setattr,gateway,'bounded_request_body',original)
        async def handler(request):
            length=0
            async for chunk in request.content.iter_chunked(65536):length+=len(chunk)
            lengths.append(length);entered.set();await release.wait()
            return web.json_response({'ok':True})
        await self.start(handler)
        async def chunks():
            for i in range(0,len(raw),65536):yield raw[i:i+65536]
        holder=self.spawn(self.post(data=chunks(),headers={'Content-Type':'application/json','Content-Length':str(len(raw))}))
        await asyncio.wait_for(entered.wait(),30)
        waiter=self.spawn(self.post(data=chunks(),headers={'Content-Type':'application/json','Content-Length':str(len(raw))}))
        await self.until(lambda:len(self.app['body_budget']._queue)==1)
        self.assertEqual(len(reads),1)
        state=await self.health()
        self.assertEqual(state['body_budget']['active_bodies'],1)
        self.assertEqual(state['body_budget']['reserved_bytes'],maximum*4)
        self.assertLessEqual(state['body_budget']['peak_reserved_bytes'],512*1024*1024)
        release.set()
        results=await asyncio.gather(holder,waiter)
        self.assertEqual([s for s,_ in results],[200,200])
        self.assertEqual(lengths,[maximum,maximum])
        self.assertEqual(self.app['body_budget'].used,0)
        self.assertEqual(self.app['body_budget'].active,0)
        self.assertEqual(len(self.app['body_budget']._queue),0)

    async def test_streaming_response_keeps_budget_then_client_cancel_releases(self):
        release=self.event()
        async def handler(request):
            await request.read()
            response=web.StreamResponse(headers={'Content-Type':'application/octet-stream'})
            await response.prepare(request);await response.write(b'x'*65536)
            await release.wait();await response.write_eof();return response
        await self.start(handler)
        response=await self.session.post(self.url+'/v1/chat/completions',headers={'Authorization':'Bearer '+KEY},json={'model':A})
        self.assertEqual(response.status,200)
        self.assertEqual(await response.content.readexactly(64),b'x'*64)
        self.assertEqual(self.app['body_budget'].active,1)
        self.assertEqual(sum(self.app['pool'].active),1)
        response.close()
        await self.until(lambda:self.app['body_budget'].used==0)
        self.assertEqual(sum(self.app['pool'].active),0)


if __name__=='__main__':unittest.main(verbosity=2)
