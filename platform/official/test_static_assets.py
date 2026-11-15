"""Public cache boundaries using real files and the actual application middleware."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
import hashlib,json,os,unittest
from fastapi import FastAPI
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from official import app_main as main
from official.static_assets import PublicAssetCache, IMMUTABLE, NO_STORE


class PublicCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary=TemporaryDirectory();self.root=Path(self.temporary.name);self.assets=self.root/'assets';self.assets.mkdir()
        self.name='index-AbC_dE12.js';self.path=self.assets/self.name;self.path.write_bytes(b'export const fixture = true;\n')
        self.manifest={'version':1,'files':{self.name:{'bytes':self.path.stat().st_size,'sha256':hashlib.sha256(self.path.read_bytes()).hexdigest()}}}
        self.write_manifest();self.cache=PublicAssetCache(self.root)
    def tearDown(self):self.temporary.cleanup()
    def write_manifest(self):(self.root/'static-assets.json').write_text(json.dumps(self.manifest))
    def policy(self,path=None,method='GET',status=200,headers=None):
        request=SimpleNamespace(method=method,url=SimpleNamespace(path=path or '/assets/'+self.name))
        return self.cache.cache_control(request,Response(status_code=status,headers=headers))
    def test_verified_hash_GET_is_public(self):self.assertEqual(self.policy(),IMMUTABLE)
    def test_verified_hash_HEAD_is_public(self):self.assertEqual(self.policy(method='HEAD'),IMMUTABLE)
    def test_verified_hash_304_retains_policy(self):self.assertEqual(self.policy(status=304),IMMUTABLE)
    def test_private_and_unversioned_paths_are_not_cached(self):
        for path in ['/','/api/me','/work-auth/sso','/api/files/private','/assets/plain.js','/assets/missing-AbC_dE12.js','/assets/../'+self.name]:
            with self.subTest(path=path):self.assertEqual(self.policy(path=path),NO_STORE)
    def test_unsafe_methods_and_error_statuses_are_not_cached(self):
        for method in ['POST','PUT','PATCH','DELETE']:
            with self.subTest(method=method):self.assertEqual(self.policy(method=method),NO_STORE)
        for status in [201,206,302,400,401,403,404,500]:
            with self.subTest(status=status):self.assertEqual(self.policy(status=status),NO_STORE)
    def test_SetCookie_and_private_variants_are_not_cached(self):
        for headers in [{'Set-Cookie':'fixture=secret'},{'Vary':'Cookie'},{'Vary':'accept-encoding, Authorization'},{'Vary':'*'},{'Content-Disposition':'attachment'}]:
            with self.subTest(headers=headers):self.assertEqual(self.policy(headers=headers),NO_STORE)
    def test_gzip_Vary_is_allowed(self):self.assertEqual(self.policy(headers={'Vary':'Accept-Encoding'}),IMMUTABLE)
    def test_modified_file_is_not_cached(self):
        self.path.write_bytes(b'changed bytes');self.assertEqual(self.policy(),NO_STORE)
    def test_same_bytes_replaced_file_is_not_cached_until_restart(self):
        original=self.path.read_bytes();self.path.unlink();self.path.write_bytes(original);self.assertEqual(self.policy(),NO_STORE)
    def test_removed_file_is_not_cached(self):
        self.path.unlink();self.assertEqual(self.policy(),NO_STORE)
    def test_unlisted_existing_hash_file_is_not_cached(self):
        (self.assets/'extra-AbC_dE12.js').write_bytes(b'private');self.assertEqual(self.policy('/assets/extra-AbC_dE12.js'),NO_STORE)
    def test_missing_manifest_fails_closed(self):
        (self.root/'static-assets.json').unlink();self.cache=PublicAssetCache(self.root);self.assertEqual(self.policy(),NO_STORE)
    def test_bad_hash_or_size_fails_closed(self):
        for field,value in [('sha256','0'*64),('bytes',999),('bytes',True)]:
            with self.subTest(field=field,value=value):
                original=self.manifest['files'][self.name][field];self.manifest['files'][self.name][field]=value;self.write_manifest();self.cache=PublicAssetCache(self.root);self.assertEqual(self.policy(),NO_STORE);self.manifest['files'][self.name][field]=original
    def test_unsafe_manifest_paths_fail_closed(self):
        for name in ['../'+self.name,'/assets/'+self.name,'unhashed.js']:
            with self.subTest(name=name):
                self.manifest['files']={name:{'bytes':self.path.stat().st_size,'sha256':hashlib.sha256(self.path.read_bytes()).hexdigest()}};self.write_manifest();self.cache=PublicAssetCache(self.root);self.assertEqual(self.policy(),NO_STORE)
    def test_invalid_JSON_and_version_fail_closed(self):
        for raw in ['not-json',json.dumps({'version':2,'files':{}}),json.dumps({'version':1,'files':[]})]:
            with self.subTest(raw=raw):
                (self.root/'static-assets.json').write_text(raw);self.cache=PublicAssetCache(self.root);self.assertEqual(self.policy(),NO_STORE)
    def test_manifest_and_asset_symlinks_fail_closed(self):
        original=self.path.read_bytes();self.path.unlink();outside=self.root/'outside.js';outside.write_bytes(original);self.path.symlink_to(outside);self.cache=PublicAssetCache(self.root);self.assertEqual(self.policy(),NO_STORE)
        self.path.unlink();self.path.write_bytes(original);manifest=self.root/'static-assets.json';saved=self.root/'saved.json';manifest.rename(saved);manifest.symlink_to(saved);self.cache=PublicAssetCache(self.root);self.assertEqual(self.policy(),NO_STORE)
    def test_actual_guard_static_GET_HEAD_and_conditional_304(self):
        app=FastAPI();app.middleware('http')(main.guard);app.mount('/assets',StaticFiles(directory=self.assets))
        with patch.object(main,'PUBLIC_ASSETS',self.cache),patch.object(main.integrated_identity,'enforce_platform_request',return_value=None),TestClient(app,base_url='http://127.0.0.1:8920')as client:
            result=client.get('/assets/'+self.name);self.assertEqual(result.status_code,200);self.assertEqual(result.headers.get_list('cache-control'),[IMMUTABLE]);self.assertIn('content-security-policy',result.headers);self.assertEqual(client.head('/assets/'+self.name).headers['cache-control'],IMMUTABLE)
            result=client.get('/assets/'+self.name,headers={'If-None-Match':result.headers['etag']});self.assertEqual(result.status_code,304);self.assertEqual(result.headers['cache-control'],IMMUTABLE)
            result=client.get('/assets/missing-AbC_dE12.js');self.assertEqual(result.status_code,404);self.assertEqual(result.headers['cache-control'],NO_STORE)
    def test_actual_guard_private_HTML_and_cookies_stay_no_store(self):
        app=FastAPI();app.middleware('http')(main.guard)
        @app.get('/')
        def index():return Response('<html>private entry</html>',media_type='text/html')
        @app.get('/api/self')
        def private():return {'self_created':True}
        @app.get('/assets/'+self.name)
        def cookie():
            response=Response('private');response.set_cookie('fixture','test');return response
        with patch.object(main,'PUBLIC_ASSETS',self.cache),patch.object(main.integrated_identity,'enforce_platform_request',return_value=None),TestClient(app,base_url='http://127.0.0.1:8920')as client:
            for path in ['/','/api/self','/assets/'+self.name]:self.assertEqual(client.get(path).headers.get_list('cache-control'),[NO_STORE])
    def test_actual_guard_rejections_stay_no_store(self):
        app=FastAPI();app.middleware('http')(main.guard)
        with TestClient(app,base_url='http://127.0.0.1:8920')as client:
            for kwargs in [{'headers':{'Origin':'https://untrusted.invalid'}},{'headers':{'Host':'untrusted.invalid'}}]:
                result=client.get('/assets/'+self.name,**kwargs);self.assertEqual(result.status_code,403);self.assertEqual(result.headers['cache-control'],NO_STORE)


if __name__=='__main__':unittest.main()
