import asyncio,importlib.util,json,pathlib,sys,types,unittest
root=pathlib.Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('removal_router_fixture',root/'wuyu_tenant_router.py');router=importlib.util.module_from_spec(spec);sys.modules[spec.name]=router;spec.loader.exec_module(router)
class Headers:
 def __init__(self,cookies):self.cookies=cookies
 def getall(self,key,default):return self.cookies if key=='Set-Cookie'else default
class Content:
 def __init__(self,body):self.body=body
 async def iter_chunked(self,size):
  for offset in range(0,len(self.body),size):yield self.body[offset:offset+size]
class Response:
 def __init__(self,value,status=200,cookies=None):self.status=status;self.headers=Headers(['fixture=only; HttpOnly']if cookies is None else cookies);self.content=Content(json.dumps(value).encode())
 async def __aenter__(self):return self
 async def __aexit__(self,*args):return None
class Client:
 def __init__(self,items,cookies=None):self.items=items;self.cookies=cookies;self.posts=[]
 def get(self,*args,**kwargs):return Response({},cookies=self.cookies)
 def post(self,url,**kwargs):self.posts.append((url,kwargs));return Response({'result':{'ok':True,'value':{'items':self.items}}})

class HistoryTests(unittest.IsolatedAsyncioTestCase):
 def setUp(self):
  self.tenant=types.SimpleNamespace(host_url='http://127.0.0.1:18151',launch_token='fixture-grant');self.item={'sessionId':'session-22222222-2222-4222-8222-222222222222','blank':False,'running':False,'updatedAt':1700000000000,'projections':{'values':{'title':'Fixture history'}}}
 async def test_history_requests_archive_filter_from_the_owned_host(self):
  client=Client([self.item]);result=await router.host_history(client,self.tenant,'fixture.invalid');self.assertEqual(result[0]['id'],self.item['sessionId']);url,request=client.posts[0];self.assertEqual(url,self.tenant.host_url+'/api/session/list');self.assertIs(request['json']['payload']['args']['_request']['excludeArchived'],True);self.assertEqual(request['headers']['Host'],'fixture.invalid');self.assertFalse(request['allow_redirects'])
 async def test_failed_owner_launch_does_not_read_history(self):
  client=Client([self.item],cookies=[])
  with self.assertRaises(router.web.HTTPServiceUnavailable):await router.host_history(client,self.tenant,'fixture.invalid')
  self.assertEqual(client.posts,[])
 async def test_invalid_native_summary_is_rejected(self):
  with self.assertRaises(router.web.HTTPServiceUnavailable):await router.host_history(Client([self.item|{'blank':'false'}]),self.tenant,'fixture.invalid')
 async def test_empty_authorized_history_stays_empty(self):
  self.assertEqual(await router.host_history(Client([]),self.tenant,'fixture.invalid'),[])

class ArchivePolicyTests(unittest.TestCase):
 def setUp(self):
  self.tenant=types.SimpleNamespace(legacy=False);self.sid='session-22222222-2222-4222-8222-222222222222'
 def request(self,value):return {'payload':{'args':{'request':value}}}
 def test_exact_own_host_archive_shape_allowed(self):
  self.assertTrue(router.rpc_allowed('workspace/archiveSession',self.request({'sessionId':self.sid}),self.tenant,router.DEFAULT_RPC))
 def test_archive_missing_id_rejected(self):
  self.assertFalse(router.rpc_allowed('workspace/archiveSession',self.request({}),self.tenant,router.DEFAULT_RPC))
 def test_archive_invalid_id_rejected(self):
  for sid in ['', '../other', 3, None]:
   with self.subTest(sid=sid):self.assertFalse(router.rpc_allowed('workspace/archiveSession',self.request({'sessionId':sid}),self.tenant,router.DEFAULT_RPC))
 def test_archive_extra_scope_fields_rejected(self):
  for key in ['cwd','workspaceId','userId','path']:
   with self.subTest(key=key):self.assertFalse(router.rpc_allowed('workspace/archiveSession',self.request({'sessionId':self.sid,key:'other'}),self.tenant,router.DEFAULT_RPC))
 def test_archive_extra_args_rejected(self):
  value=self.request({'sessionId':self.sid});value['payload']['args']['other']={};self.assertFalse(router.rpc_allowed('workspace/archiveSession',value,self.tenant,router.DEFAULT_RPC))
 def test_archive_malformed_envelopes_rejected(self):
  for value in [{},{'payload':None},{'payload':{'args':None}},{'payload':{'args':{'request':[]}}}]:
   with self.subTest(value=value):self.assertFalse(router.rpc_allowed('workspace/archiveSession',value,self.tenant,router.DEFAULT_RPC))
 def test_unrelated_optional_rpcs_stay_denied(self):
  for method in ['subagents/list','commands/list','workspace/create','workspace/delete','workspace/rename']:
   with self.subTest(method=method):self.assertFalse(router.rpc_allowed(method,self.request({'sessionId':self.sid}),self.tenant,router.DEFAULT_RPC))
 def test_explicit_custom_allowlist_can_keep_archive_disabled(self):
  self.assertFalse(router.rpc_allowed('workspace/archiveSession',self.request({'sessionId':self.sid}),self.tenant,{'session/list'}))

if __name__=='__main__':unittest.main()
