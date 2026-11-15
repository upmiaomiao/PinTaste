from __future__ import annotations
import io
import re
import time
from collections import OrderedDict
from urllib.parse import urlparse
import httpx


class ScientificAssetReadError(OSError):
    """Credential-safe read failure; signed URLs and provider bodies stay private."""
    def __init__(self, code, *, status_code=None, retryable=False):
        self.code, self.status_code, self.retryable = code, status_code, retryable
        super().__init__(code)

def _safe_url(url):
    parsed = urlparse(url)
    return parsed.scheme == 'https' and not parsed.username and not parsed.password and (parsed.hostname or '').endswith('.blob.core.windows.net') and parsed.port in (None,443)

class HttpRangeReader(io.RawIOBase):
    """Bounded, seekable HTTP range file for rasterio's Python VSI opener.

    This also avoids Windows GDAL/cURL proxy and locale compatibility problems.
    Range support is mandatory; a 200 response must never load a whole scene.
    """
    BLOCK_SIZE = 256 * 1024
    MAX_TRANSFER = 32 * 1024 * 1024

    def __init__(self, url: str, client=None):
        super().__init__()
        if not _safe_url(url):
            raise ValueError("Unsupported range asset host")
        self._url, self._pos, self._size = url, 0, None
        # Only validated Azure Blob URLs reach this reader. The system proxy
        # intermittently breaks these large range responses on Windows.
        self._client = client or httpx.Client(timeout=30, follow_redirects=False, trust_env=False)
        self._owns_client = client is None
        self._cache = OrderedDict()
        self.transferred_bytes = 0
        self.range_requests = self.transport_retries = 0
        self.last_error = None
        try:
            self._block(0)
        except Exception:
            self.close()
            raise

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self._pos

    def seek(self, offset, whence=0):
        target = offset if whence == 0 else self._pos + offset if whence == 1 else self._size + offset if whence == 2 else -1
        if target < 0:
            raise ValueError("Invalid seek")
        self._pos = target
        return target

    def _block(self, index):
        if index in self._cache:
            self._cache.move_to_end(index)
            return self._cache[index]
        start = index * self.BLOCK_SIZE
        if self._size is not None and start >= self._size:
            return b""
        stop = start + self.BLOCK_SIZE - 1
        if self._size is not None:
            stop = min(stop, self._size - 1)
        for attempt in range(3):
            try:
                value = self._fetch_block(start, stop)
                break
            except httpx.TransportError:
                error = ScientificAssetReadError('scientific_asset_transport_error', retryable=True)
            except ScientificAssetReadError as exc:
                error = exc
            self.last_error = error
            # Two retries for the entire reader, including repeated GDAL reads;
            # partial response bytes still count towards the transfer ceiling.
            if not error.retryable or self.transport_retries >= 2 or attempt == 2:
                raise error from None
            self.transport_retries += 1
            time.sleep(0.25 * self.transport_retries)
        self._cache[index] = value
        self.last_error = None
        if len(self._cache) > 16:
            self._cache.popitem(last=False)
        return value

    def _fetch_block(self, start, stop):
        self.range_requests += 1
        with self._client.stream("GET", self._url, headers={"Range": f"bytes={start}-{stop}"}) as response:
            if response.status_code in {401, 403}:
                raise ScientificAssetReadError('scientific_asset_signature_rejected', status_code=response.status_code)
            if response.status_code in {408, 429, 500, 502, 503, 504}:
                raise ScientificAssetReadError('scientific_asset_http_transient', status_code=response.status_code, retryable=True)
            if response.status_code != 206:
                raise OSError("Scientific asset does not provide bounded HTTP byte ranges")
            match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("content-range", ""))
            if not match or int(match[1]) != start:
                raise OSError("Invalid range response")
            total = int(match[3])
            if self._size is not None and total != self._size:
                raise OSError("Asset size changed during crop")
            self._size = total
            data = bytearray()
            for chunk in response.iter_bytes():
                data.extend(chunk)
                self.transferred_bytes += len(chunk)
                if len(data) > self.BLOCK_SIZE or self.transferred_bytes > self.MAX_TRANSFER:
                    raise OSError("SAR bounded transfer limit exceeded")
            if len(data) != int(match[2]) - start + 1:
                raise ScientificAssetReadError('scientific_asset_truncated_range', retryable=True)
        return bytes(data)

    def read(self, size=-1):
        if size is None or size < 0:
            size = self._size - self._pos
        if size > self.MAX_TRANSFER:
            raise OSError("Whole scene reads are disabled")
        size = min(size, max(0, self._size - self._pos))
        result = bytearray()
        while size:
            index, offset = divmod(self._pos, self.BLOCK_SIZE)
            block = self._block(index)
            amount = min(size, len(block) - offset)
            if amount <= 0:
                break
            result.extend(block[offset:offset + amount])
            self._pos += amount
            size -= amount
        return bytes(result)

    def readinto(self, target):
        value = self.read(len(target))
        target[:len(value)] = value
        return len(value)

    def close(self):
        if not self.closed:
            getattr(self, "_cache", {}).clear()
            if getattr(self, "_owns_client", False):
                self._client.close()
        super().close()
