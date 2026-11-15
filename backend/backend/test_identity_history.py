"""History supports explicit staging ports while preserving credential boundaries."""
import json
import unittest
from unittest.mock import patch
import uuid

import httpx
from services import wuyu_identity as identity


class IdentityHistoryTests(unittest.TestCase):
    def settings(self,url,port=None):
        values={'WUYU_TENANT_ROUTER_URL':url}
        if port is not None:values['WUYU_TENANT_ROUTER_EXPECTED_PORT']=port
        return patch.object(identity,'config',side_effect=lambda key,default='':values.get(key,default))

    def test_original_default_stays_on_explicit_internal_8150(self):
        for host in ('127.0.0.1','host.docker.internal'):
            with self.settings('http://'+host+':8150/'):
                self.assertEqual(identity.tenant_router_url(),'http://'+host+':8150')

    def test_staging_port_requires_matching_server_selection(self):
        with self.settings('http://host.docker.internal:18150',18150):
            self.assertEqual(identity.tenant_router_url(),'http://host.docker.internal:18150')
        with self.settings('http://host.docker.internal:18150'):
            with self.assertRaises(identity.IdentityFailure):identity.tenant_router_url()

    def test_server_selection_is_not_an_arbitrary_port_allowlist(self):
        with self.settings('http://127.0.0.1:31500','31500'):
            self.assertEqual(identity.tenant_router_url(),'http://127.0.0.1:31500')
        with self.settings('http://127.0.0.1:18150','31500'):
            with self.assertRaises(identity.IdentityFailure):identity.tenant_router_url()

    def test_invalid_port_configuration_fails_before_any_http_request(self):
        for port in ('',0,65536,'-1','18150.0',' 18150','١٨١٥٠',True,'999999'):
            with self.subTest(port=port),self.settings('http://host.docker.internal:18150',port),patch.object(identity.httpx,'Client') as client:
                with self.assertRaises(identity.IdentityFailure):identity.work_history('fixture-token','fixture-user')
                client.assert_not_called()

    def test_external_hosts_credentials_paths_and_injection_cannot_receive_keys(self):
        urls=['https://host.docker.internal:18150','http://evil.test:18150','http://localhost:18150',
              'http://host.docker.internal.evil.test:18150','http://user:pass@host.docker.internal:18150',
              'http://@host.docker.internal:18150','http://:@host.docker.internal:18150',
              'http://host.docker.internal:18150/path','http://host.docker.internal:18150?x=1',
              'http://host.docker.internal:18150?','http://host.docker.internal:18150#',
              'http://host.docker.internal:18150/#x','http://host.docker.internal:bad',
              'http://host.docker.internal:65536','http://host.docker.internal',
              'http://host.docker.internal:18150/\\evil','\nhttp://host.docker.internal:18150',
              'http://host.docker.internal:18150\r\nX:y','http://[invalid:18150']
        for url in urls:
            with self.subTest(url=url),self.settings(url,18150),patch.object(identity.httpx,'Client') as client:
                with self.assertRaises(identity.IdentityFailure):identity.work_history('fixture-token','fixture-user')
                client.assert_not_called()

    def request(self,response):
        original=httpx.Client;calls=[];options=[]
        def handler(request):calls.append(request);return response
        def client(**kwargs):options.append(kwargs);return original(transport=httpx.MockTransport(handler),**kwargs)
        with self.settings('http://host.docker.internal:18150',18150),patch.object(identity,'shared_key',return_value='fixture-private-key'),patch.object(identity.httpx,'Client',side_effect=client):
            result=identity.work_history('fixture-token','fixture principal/?')
        return result,calls,options

    def item(self):
        return {'id':'session-'+str(uuid.uuid4()),'blank':False,'running':False,'updated_at':1000}

    def test_real_request_keeps_headers_path_encoding_and_response_contract(self):
        item=self.item();result,calls,options=self.request(httpx.Response(200,json={'items':[item]}))
        self.assertEqual(result,[item]);self.assertEqual(len(calls),1)
        request=calls[0]
        self.assertEqual(str(request.url).split('?')[0],'http://host.docker.internal:18150/__internal/identity/history')
        self.assertEqual(request.url.params['work_user_id'],'fixture principal/?')
        self.assertEqual(request.headers['Authorization'],'Bearer fixture-token')
        self.assertEqual(request.headers['X-Wuyu-Identity-Key'],'fixture-private-key')
        self.assertFalse(options[0]['follow_redirects']);self.assertFalse(options[0]['trust_env'])

    def test_redirect_and_upstream_failures_remain_rejected(self):
        for status in (302,401,503):
            with self.subTest(status=status),self.assertRaises(identity.IdentityFailure) as raised:
                self.request(httpx.Response(status,headers={'Location':'https://evil.test'}))
            self.assertEqual(raised.exception.code,'WORK_HISTORY_UNAVAILABLE')

    def test_malformed_history_is_not_treated_as_success(self):
        for body in ({},{'items':{}},{'items':[{'id':'not-a-session'}]}):
            with self.subTest(body=body),self.assertRaises(identity.IdentityFailure):self.request(httpx.Response(200,json=body))

    def test_nonfinite_timestamps_remain_rejected(self):
        item=self.item();item['updated_at']=float('nan')
        with self.assertRaises(identity.IdentityFailure):self.request(httpx.Response(200,content=json.dumps({'items':[item]}).encode()))

    def test_oversized_history_remains_rejected(self):
        with self.assertRaises(identity.IdentityFailure) as raised:
            self.request(httpx.Response(200,content=b'x'*(4*1024*1024+1)))
        self.assertEqual(raised.exception.code,'WORK_HISTORY_TOO_LARGE')


if __name__=='__main__':unittest.main()
