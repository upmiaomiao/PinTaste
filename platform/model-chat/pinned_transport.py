"""Optional curl-compatible SPKI pinning before sending any HTTP credentials."""
import base64
import hashlib
import hmac
import re
from urllib.parse import urlsplit

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import serialization


def validate_pin(value, base_url):
    if not isinstance(value, str) or not re.fullmatch(r'sha256//[A-Za-z0-9+/]{43}=', value):
        raise ValueError('TLS公钥固定值须为sha256//格式。')
    if urlsplit(base_url).scheme != 'https' or len(base64.b64decode(value[8:], validate=True)) != 32:
        raise ValueError('TLS公钥固定校验仅支持HTTPS。')
    return value


def certificate_pin(der):
    cert = x509.load_der_x509_certificate(der)
    key = cert.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return 'sha256//' + base64.b64encode(hashlib.sha256(key).digest()).decode('ascii')


class PinnedTransport(httpx.AsyncBaseTransport):
    """Match -k --pinnedpubkey without an unverified HTTP request.

    HTTPCore's public trace callback runs after TLS and before HTTP headers.
    A fresh HTTP/1 connection per request keeps the verified state local to that
    request. Missing TLS/trace information fails closed before headers too.
    """
    def __init__(self, base_url, pin):
        self.pin = validate_pin(pin, base_url)
        parsed = httpx.URL(base_url)
        self.origin = (parsed.scheme, parsed.host, parsed.port)
        self.transport = httpx.AsyncHTTPTransport(verify=False, trust_env=False,
            http2=False, retries=0, limits=httpx.Limits(max_keepalive_connections=0))

    async def handle_async_request(self, request):
        if (request.url.scheme, request.url.host, request.url.port) != self.origin:
            raise httpx.ConnectError('TLS_PIN_ORIGIN_MISMATCH', request=request)
        verified = False
        previous = request.extensions.get('trace')

        async def trace(name, info):
            nonlocal verified
            if name == 'connection.start_tls.complete':
                stream = info.get('return_value')
                try:
                    ssl_object = stream.get_extra_info('ssl_object')
                    actual = certificate_pin(ssl_object.getpeercert(binary_form=True))
                except Exception:
                    if stream is not None:
                        await stream.aclose()
                    raise httpx.ConnectError('TLS_PUBLIC_KEY_PIN_UNAVAILABLE', request=request) from None
                if not hmac.compare_digest(actual, self.pin):
                    await stream.aclose()
                    raise httpx.ConnectError('TLS_PUBLIC_KEY_PIN_MISMATCH', request=request)
                verified = True
            if name in ('http11.send_request_headers.started', 'http2.send_request_headers.started') and not verified:
                raise httpx.ConnectError('TLS_PUBLIC_KEY_PIN_UNVERIFIED', request=request)
            if previous is not None:
                await previous(name, info)

        request.extensions['trace'] = trace
        return await self.transport.handle_async_request(request)

    async def aclose(self):
        await self.transport.aclose()


def model_client_options(profile):
    pin = profile.get('tls_public_key_pin', '')
    return {'transport': PinnedTransport(profile['base_url'], pin)} if pin else {}
