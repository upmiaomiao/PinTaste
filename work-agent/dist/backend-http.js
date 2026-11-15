/** Backend-only HTTP transport. No redirects, connection reuse, or paid retries.
 * Uses node:http rather than the runtime's Undici response parser. All socket
 * failures remain local to the request/body; never install process-wide handlers.
 */
import http from 'node:http';
import https from 'node:https';
import { Readable } from 'node:stream';
import { pipeline } from 'node:stream/promises';
import { createUnzip, createBrotliDecompress } from 'node:zlib';
import { randomUUID } from 'node:crypto';

export class BackendHttpError extends Error {
    constructor(code, phase, requestId, name = 'Error') {
        super('Backend HTTP request could not be completed');
        this.name = name;
        this.code = code;
        this.phase = phase;
        this.requestId = requestId;
    }
}

export async function backendFetch(url, init = {}) {
    const target = new URL(url);
    if (!['http:', 'https:'].includes(target.protocol) || target.username || target.password)
        throw new TypeError('Invalid backend service URL');
    if (init.redirect !== 'error')
        throw new TypeError('Backend redirects must be disabled');
    const encoded = new Request(target, init);
    const signal = init.signal;
    signal?.throwIfAborted();
    const headers = Object.fromEntries(encoded.headers);
    // Internal APIs normally send identity bytes. Decode a forced encoding safely
    // too; unlike fetch, never retain an encoded Content-Length on decoded bytes.
    if (!encoded.headers.has('accept-encoding')) headers['accept-encoding'] = 'identity';
    const requestId = randomUUID();
    let request, incoming, decoder, upload, responseController;
    let headersReceived = false, finished = false, cancelled = false, logged = false;
    const cleanup = () => signal?.removeEventListener('abort', abort);
    const error = (code, phase, name) => {
        const failure = new BackendHttpError(code, phase, requestId, name);
        if (!logged && code !== 'BACKEND_REQUEST_ABORTED') {
            logged = true;
            console.warn(`MCP backend HTTP failed request=${requestId} method=${encoded.method} phase=${phase} code=${code}`);
        }
        return failure;
    };
    const destroy = (failure) => {
        upload?.destroy(failure);
        decoder?.destroy(failure);
        incoming?.destroy(failure);
        request?.destroy(failure);
    };
    let rejectHeaders;
    const fail = (failure) => {
        if (finished || cancelled) return;
        finished = true;
        cleanup();
        if (headersReceived) responseController?.error(failure);
        else rejectHeaders?.(failure);
        destroy(failure);
    };
    const abort = () => fail(error(signal?.reason?.name === 'TimeoutError'
        ? 'BACKEND_REQUEST_TIMEOUT' : 'BACKEND_REQUEST_ABORTED', headersReceived ? 'body' : 'headers',
        signal?.reason?.name === 'TimeoutError' ? 'TimeoutError' : 'AbortError'));
    return new Promise((resolve, reject) => {
        rejectHeaders = reject;
        signal?.addEventListener('abort', abort, { once: true });
        if (signal?.aborted) return abort();
        const transport = target.protocol === 'https:' ? https : http;
        try {
        request = transport.request(target, { method: encoded.method, headers, agent: false,
            maxHeaderSize: 16 * 1024 }, (response) => {
            try {
            incoming = response;
            // Attach immediately: a peer may fail before a consumer reads body.
            incoming.on('error', () => {});
            if (finished || cancelled) return incoming.destroy();
            const status = incoming.statusCode ?? 502;
            if (status < 200 || status > 599)
                return fail(error('BACKEND_RESPONSE_INVALID', 'headers'));
            if ([301, 302, 303, 307, 308].includes(status) && incoming.headers.location)
                return fail(error('BACKEND_REDIRECT_REJECTED', 'headers'));
            const outputHeaders = new Headers();
            for (let i = 0; i < incoming.rawHeaders.length; i += 2)
                outputHeaders.append(incoming.rawHeaders[i], incoming.rawHeaders[i + 1]);
            const noBody = encoded.method === 'HEAD' || [204, 205, 304].includes(status);
            const coding = (outputHeaders.get('content-encoding') || 'identity').trim().toLowerCase();
            if (!noBody && coding !== 'identity') {
                if (status === 206 || !['gzip', 'x-gzip', 'deflate', 'br'].includes(coding))
                    return fail(error('BACKEND_RESPONSE_ENCODING_UNSUPPORTED', 'headers'));
                decoder = coding === 'br' ? createBrotliDecompress() : createUnzip();
                decoder.on('error', () => {});
                incoming.on('error', () => {
                    if (!finished && !cancelled) decoder.destroy(error('BACKEND_RESPONSE_TRUNCATED', 'body'));
                });
                incoming.pipe(decoder);
                outputHeaders.delete('content-encoding');
                outputHeaders.delete('content-length');
            }
            if (noBody) {
                finished = true;
                cleanup();
                incoming.destroy();
                return resolve(new Response(null, { status, headers: outputHeaders }));
            }
            const source = decoder || incoming;
            const iterator = source[Symbol.asyncIterator]();
            const body = new ReadableStream({
                start(controller) { responseController = controller; },
                async pull(controller) {
                    try {
                        const part = await iterator.next();
                        if (finished || cancelled) return;
                        if (part.done) {
                            if (!incoming.complete) return fail(error('BACKEND_RESPONSE_TRUNCATED', 'body'));
                            finished = true;
                            cleanup();
                            controller.close();
                        } else controller.enqueue(new Uint8Array(part.value.buffer, part.value.byteOffset, part.value.byteLength));
                    } catch {
                        if (!finished && !cancelled) fail(error('BACKEND_RESPONSE_TRUNCATED', 'body'));
                    }
                },
                cancel() {
                    cancelled = true;
                    cleanup();
                    destroy();
                },
            });
            headersReceived = true;
            resolve(new Response(body, { status, headers: outputHeaders }));
            } catch {
                fail(error('BACKEND_RESPONSE_INVALID', 'headers'));
            }
        });
        } catch {
            fail(error('BACKEND_REQUEST_INVALID', 'headers'));
            return;
        }
        request.on('error', () => {
            if (!finished && !cancelled) fail(error(headersReceived ? 'BACKEND_RESPONSE_TRUNCATED' : 'BACKEND_NETWORK_ERROR', headersReceived ? 'body' : 'headers'));
        });
        request.on('upgrade', (_response, socket) => {
            socket.destroy();
            fail(error('BACKEND_UPGRADE_REJECTED', 'headers'));
        });
        if (encoded.body) {
            upload = Readable.fromWeb(encoded.body);
            // A pipeline owns every upload error and cancellation. Its rejection
            // is handled even when the caller is already reading the response.
            pipeline(upload, request).catch(() => {
                if (!finished && !cancelled) fail(error('BACKEND_UPLOAD_INTERRUPTED', 'upload'));
            });
        } else request.end();
    });
}
