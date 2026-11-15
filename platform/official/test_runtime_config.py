"""Environment switching and origin rejection without network or real secrets."""
import unittest

from official.runtime_config import RuntimeConfig


class RuntimeConfigTests(unittest.TestCase):
    def test_same_code_builds_links_for_each_environment(self):
        for origin in ['https://ai.wuyullm.com', 'https://test-ai.wuyullm.com', 'https://preview.example.test:9443']:
            with self.subTest(origin=origin):
                config = RuntimeConfig.from_env({'WUYU_PUBLIC_ORIGIN': origin})
                self.assertEqual(config.public_url('/work-auth/sso?next=%2Fwork'), origin+'/work-auth/sso?next=%2Fwork')
                self.assertEqual(config.public_url('/?conversation=abc'), origin+'/?conversation=abc')
                self.assertEqual(config.public_url('/work?session=session-abc'), origin+'/work?session=session-abc')

    def test_local_development_defaults_keep_both_loopback_addresses(self):
        config = RuntimeConfig.from_env({})
        self.assertEqual(config.public_origin, 'http://127.0.0.1:8920')
        self.assertEqual(config.hosts, frozenset({'127.0.0.1:8920', 'localhost:8920'}))

    def test_canonical_origin_and_existing_internal_aliases(self):
        config = RuntimeConfig.from_env({'WUYU_PUBLIC_ORIGIN':'https://TEST-AI.WUYULLM.COM:443/',
            'WUYU_ALLOWED_ORIGINS':'https://test-ai.wuyullm.com, http://ai.wuyullm.com,http://wuyu-platform:8920'})
        self.assertEqual(config.public_origin,'https://test-ai.wuyullm.com')
        self.assertIn('wuyu-platform:8920', config.hosts)
        self.assertIn('ai.wuyullm.com',config.hosts)

    def test_invalid_public_origins_fail_before_application_start(self):
        for value in ['', 'http://example.test', 'https://user:pass@example.test',
                      'https://example.test/path', 'https://example.test?key=x',
                      'https://example.test#fragment', 'https://example.test?',
                      'https://example.test#', '//example.test', 'javascript:alert(1)',
                      'https://example.test:99999', 'https://example.test:zero',
                      'https://example.test:0', 'https://example.test:',
                      'https://exam\tple.test', 'https://example.test\\evil']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                RuntimeConfig.from_env({'WUYU_PUBLIC_ORIGIN':value})

    def test_canonical_public_origin_must_be_allowed(self):
        with self.assertRaises(ValueError):
            RuntimeConfig.from_env({'WUYU_PUBLIC_ORIGIN':'https://preview.example.test',
                'WUYU_ALLOWED_ORIGINS':'https://test-ai.wuyullm.com'})

    def test_invalid_or_empty_allowlist_entries_are_rejected(self):
        for value in ['', 'https://example.test,', 'https://example.test,https://other.test/path']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                RuntimeConfig.from_env({'WUYU_PUBLIC_ORIGIN':'https://example.test','WUYU_ALLOWED_ORIGINS':value})

    def test_execution_modes_are_explicit_and_invalid_modes_fail_startup(self):
        self.assertEqual(RuntimeConfig.from_env({}).chat_execution_mode, 'combined')
        for mode in ('combined', 'api', 'worker'):
            self.assertEqual(RuntimeConfig.from_env({'WUYU_CHAT_EXECUTION_MODE':mode}).chat_execution_mode, mode)
        for mode in ('', 'API', 'all'):
            with self.assertRaises(ValueError):
                RuntimeConfig.from_env({'WUYU_CHAT_EXECUTION_MODE':mode})

    def test_cookie_defaults_are_host_only_and_environment_neutral(self):
        config = RuntimeConfig.from_env({})
        self.assertIsNone(config.cookie_domain)
        self.assertEqual((config.session_cookie, config.legacy_session_cookie, config.work_session_cookie),
                         ('wuyu_identity_session', 'wuyu_session', 'banana_auth_token'))

    def test_explicit_staging_cookie_contract_is_preserved(self):
        config = RuntimeConfig.from_env({'WUYU_PUBLIC_ORIGIN':'https://test-ai.wuyullm.com',
            'WUYU_SESSION_COOKIE':'wuyu_staging_identity_session',
            'WUYU_LEGACY_SESSION_COOKIE':'wuyu_staging_session',
            'WUYU_WORK_SESSION_COOKIE':'banana_staging_auth_token',
            'WUYU_COOKIE_DOMAIN':'TEST-AI.WUYULLM.COM'})
        self.assertEqual(config.cookie_domain, 'test-ai.wuyullm.com')
        self.assertEqual(config.session_cookie, 'wuyu_staging_identity_session')
        self.assertEqual(config.legacy_session_cookie, 'wuyu_staging_session')
        self.assertEqual(config.work_session_cookie, 'banana_staging_auth_token')

    def test_cookie_names_reject_header_injection_and_collisions(self):
        for key in ('WUYU_SESSION_COOKIE', 'WUYU_LEGACY_SESSION_COOKIE', 'WUYU_WORK_SESSION_COOKIE'):
            for value in ('', 'bad name', 'x;y', 'x=y', 'x,y', 'x:y', 'x\r\nSet-Cookie:y', '中文', 'x'*129, 'Path', 'domain', 'Secure', 'SameSite'):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    RuntimeConfig.from_env({key:value})
        for key in ('WUYU_LEGACY_SESSION_COOKIE', 'WUYU_WORK_SESSION_COOKIE'):
            with self.assertRaises(ValueError):
                RuntimeConfig.from_env({key:'wuyu_identity_session'})

    def test_cookie_domain_cannot_expand_to_other_environments(self):
        for domain in ('wuyullm.com', '.test-ai.wuyullm.com', 'ai.wuyullm.com',
                       'test-ai.wuyullm.com:443', 'https://test-ai.wuyullm.com',
                       'test-ai.wuyullm.com/path', 'test-ai.wuyullm.com\r\nX:y'):
            with self.subTest(domain=domain), self.assertRaises(ValueError):
                RuntimeConfig.from_env({'WUYU_PUBLIC_ORIGIN':'https://test-ai.wuyullm.com', 'WUYU_COOKIE_DOMAIN':domain})
        for origin, domain in (('http://127.0.0.1:8920','127.0.0.1'), ('http://localhost:8920','localhost')):
            with self.assertRaises(ValueError):
                RuntimeConfig.from_env({'WUYU_PUBLIC_ORIGIN':origin, 'WUYU_COOKIE_DOMAIN':domain})

    def test_host_prefix_requires_host_only_domain(self):
        self.assertEqual(RuntimeConfig.from_env({'WUYU_SESSION_COOKIE':'__Host-session'}).session_cookie, '__Host-session')
        with self.assertRaises(ValueError):
            RuntimeConfig.from_env({'WUYU_PUBLIC_ORIGIN':'https://preview.example.test',
                'WUYU_SESSION_COOKIE':'__Host-session', 'WUYU_COOKIE_DOMAIN':'preview.example.test'})

    def test_existing_production_parent_requires_explicit_operator_setting(self):
        env = {'WUYU_PUBLIC_ORIGIN': 'https://ai.wuyullm.com', 'WUYU_COOKIE_DOMAIN': '.wuyullm.com'}
        with self.assertRaises(ValueError):
            RuntimeConfig.from_env(env)
        configured = env | {'WUYU_COOKIE_PARENT_DOMAIN': 'wuyullm.com'}
        self.assertEqual(RuntimeConfig.from_env(configured).cookie_domain, '.wuyullm.com')
        self.assertEqual(RuntimeConfig.from_env(configured | {'WUYU_COOKIE_DOMAIN': 'wuyullm.com'}).cookie_domain, 'wuyullm.com')

    def test_explicit_parent_rejects_suffix_confusion_wrong_scope_and_injection(self):
        base = {'WUYU_COOKIE_PARENT_DOMAIN': 'wuyullm.com', 'WUYU_COOKIE_DOMAIN': '.wuyullm.com',
                'WUYU_PUBLIC_ORIGIN': 'https://ai.wuyullm.com'}
        for origin in ['https://notwuyullm.com', 'https://wuyullm.com.attacker.invalid']:
            with self.assertRaises(ValueError):
                RuntimeConfig.from_env(base | {'WUYU_PUBLIC_ORIGIN': origin})
        for domain in ['..wuyullm.com', '.other.test', '.wuyullm.com\r\nX:y']:
            with self.assertRaises(ValueError):
                RuntimeConfig.from_env(base | {'WUYU_COOKIE_DOMAIN': domain})

    def test_parent_exception_does_not_allow_ip_or_host_prefixed_cookies(self):
        for env in [
            {'WUYU_PUBLIC_ORIGIN': 'http://127.0.0.1:8920', 'WUYU_COOKIE_PARENT_DOMAIN': '0.0.1', 'WUYU_COOKIE_DOMAIN': '.0.0.1'},
            {'WUYU_PUBLIC_ORIGIN': 'https://ai.wuyullm.com', 'WUYU_COOKIE_PARENT_DOMAIN': 'wuyullm.com',
             'WUYU_COOKIE_DOMAIN': '.wuyullm.com', 'WUYU_SESSION_COOKIE': '__Host-session'},
        ]:
            with self.assertRaises(ValueError):
                RuntimeConfig.from_env(env)

    def test_queue_warning_threshold_is_validated(self):
        self.assertEqual(RuntimeConfig.from_env({}).task_queue_warn_seconds, 300)
        for value in ('30', '300', '86400'):
            self.assertEqual(RuntimeConfig.from_env({'WUYU_TASK_QUEUE_WARN_SECONDS':value}).task_queue_warn_seconds, int(value))
        for value in ('', '0', '29', '86401', '300.0', '-1', 'nan', ' 300', '999999999'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                RuntimeConfig.from_env({'WUYU_TASK_QUEUE_WARN_SECONDS':value})

    def test_links_cannot_override_the_configured_origin(self):
        config=RuntimeConfig.from_env({'WUYU_PUBLIC_ORIGIN':'https://preview.example.test'})
        for path in ['https://evil.test/work','//evil.test/work','/\\evil.test','/work#fragment','/work\r\nLocation:evil']:
            with self.subTest(path=path), self.assertRaises(ValueError):
                config.public_url(path)


if __name__ == '__main__':
    unittest.main()
