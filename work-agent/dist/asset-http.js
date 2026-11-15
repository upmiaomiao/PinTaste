import { Readable, Transform } from 'node:stream';
import { pipeline } from 'node:stream/promises';
import { BananaApiError } from './api-client.js';
import { verifyAssetToken } from './tokens.js';
import { referenceLibrary } from './reference-library.js';
export function registerAssetRoutes(app, config, api) {
    app.get(['/assets', '/assets/:filename'], async (req, res) => {
        const abort = new AbortController();
        const disconnect = () => { if (!res.writableEnded)
            abort.abort(); };
        res.once('close', disconnect);
        let authorized = false;
        try {
            if (typeof req.query.token !== 'string')
                return void res.status(403).end();
            const cap = verifyAssetToken(req.query.token, config.capabilitySecret);
            authorized = true;
            res.setHeader('X-Content-Type-Options', 'nosniff');
            res.setHeader('Referrer-Policy', 'no-referrer');
            res.setHeader('Cache-Control', 'private, no-store');
            res.setHeader('Content-Security-Policy', "sandbox allow-downloads; default-src 'none'");
            if (cap.scope === 'library') {
                const image = await referenceLibrary.image(cap.path);
                return void res.type(image.contentType).send(image.data);
            }
            const prefix = `/api/projects/${cap.projectId}/workflow/artifacts/`;
            if (!cap.path.startsWith(prefix) || !/^[\w-]+(?:\?download=1)?$/.test(cap.path.slice(prefix.length)))
                return void res.status(403).end();
            const file = await api.getBinaryStream(cap.path, abort.signal, req.header('range'));
            const type = (file.headers.get('content-type') || 'application/octet-stream').split(';')[0].toLowerCase();
            const allowed = /^(image\/(png|jpeg|webp|gif)|application\/(pdf|zip|json|octet-stream|vnd\.openxmlformats-officedocument\.presentationml\.presentation)|text\/(plain|markdown))$/.test(type);
            res.status(file.status).type(allowed ? type : 'application/octet-stream');
            if (cap.path.includes('download=1') || !allowed)
                res.setHeader('Content-Disposition', file.headers.get('content-disposition') || 'attachment');
            for (const header of ['content-length', 'content-range', 'accept-ranges']) {
                const value = file.headers.get(header);
                if (value)
                    res.setHeader(header, value);
            }
            if (!file.body)
                return void res.end();
            let bytes = 0;
            await pipeline(Readable.fromWeb(file.body), new Transform({ transform(chunk, _encoding, next) {
                    bytes += chunk.length;
                    next(bytes > 200 * 1024 * 1024 ? new Error('Artifact exceeds limit') : null, chunk);
                } }), res, { signal: abort.signal });
        }
        catch (error) {
            if (res.headersSent)
                res.destroy();
            else if (!res.destroyed) {
                const status = !authorized ? 403 : error instanceof BananaApiError && [401, 403, 404, 416].includes(error.status) ? error.status : 503;
                res.status(status).json({ error: status === 503 ? '下载服务暂不可用，文件仍保存在项目中，请稍后重试。' : '文件链接已失效或请求范围无效，请返回项目重新下载。' });
            }
        }
        finally {
            res.removeListener('close', disconnect);
        }
    });
}
