import { createWriteStream } from "node:fs";
import { mkdtemp, unlink, rmdir } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Transform } from "node:stream";
import { pipeline } from "node:stream/promises";
import { createHash, randomUUID } from "node:crypto";
import { BananaApiError } from "./api-client.js";
import { projectIdFromHandle } from "./tokens.js";
export const DEFAULT_MATERIAL_UPLOAD_LIMITS = { max_file_bytes: 50 * 1024 * 1024 - 1, max_project_files: 50, max_batch_files: 50, max_batch_bytes: 500 * 1024 * 1024 };
class UploadFailure extends Error {
    status;
    code;
    constructor(status, code) {
        super(code);
        this.status = status;
        this.code = code;
    }
}
/** op180: browsers may send up to five files at once; the backend registers one file per project at a time. */
const registrations = new Map();
export function serializedRegistration(projectId, task) {
    const prior = registrations.get(projectId) || Promise.resolve();
    const run = prior.then(task, task);
    const tail = run.then(() => { }, () => { });
    registrations.set(projectId, tail);
    tail.then(() => { if (registrations.get(projectId) === tail)
        registrations.delete(projectId); });
    return run;
}
const record = (v) => v !== null && typeof v === "object" && !Array.isArray(v) ? v : {};
const positive = (v) => Number.isSafeInteger(Number(v)) && Number(v) > 0;
function limitsOf(config) {
    const limits = { ...DEFAULT_MATERIAL_UPLOAD_LIMITS, ...config.materialUploadLimits };
    if (Object.values(limits).some(v => !positive(v)) || limits.max_batch_bytes < limits.max_file_bytes || limits.max_batch_files > limits.max_project_files)
        throw new Error("Invalid material upload limits");
    return limits;
}
function metadata(req, limits) {
    let filename;
    try {
        filename = decodeURIComponent(req.header("x-filename") || "");
    }
    catch {
        throw new UploadFailure(400, "INVALID_UPLOAD_METADATA");
    }
    const size = Number(req.header("x-file-size")), count = Number(req.header("x-batch-count")), bytes = Number(req.header("x-batch-bytes")), role = req.header("x-material-role"), id = req.header("x-upload-id");
    if (!filename || filename.length > 180 || /[\\/\x00-\x1f]/.test(filename) || /^\.+$/.test(filename) || !positive(size) || !["material", "template", "logo"].includes(role || "") || !/^[\da-f]{8}-[\da-f]{4}-4[\da-f]{3}-[89ab][\da-f]{3}-[\da-f]{12}$/i.test(id || ""))
        throw new UploadFailure(400, "INVALID_UPLOAD_METADATA");
    if (size > limits.max_file_bytes)
        throw new UploadFailure(413, "MATERIAL_UPLOAD_LIMIT");
    if (!positive(count) || count > limits.max_batch_files || !positive(bytes) || bytes < size || bytes > limits.max_batch_bytes)
        throw new UploadFailure(413, "MATERIAL_BATCH_LIMIT");
    const declared = req.header("content-length");
    if (declared && Number(declared) !== size)
        throw new UploadFailure(400, "UPLOAD_LENGTH_MISMATCH");
    if (!/^application\/octet-stream(?:;|$)/i.test(req.header("content-type") || ""))
        throw new UploadFailure(415, "BINARY_FILE_REQUIRED");
    const mime = req.header("x-file-type") || "application/octet-stream";
    if (mime.length > 100 || /[\r\n]/.test(mime))
        throw new UploadFailure(400, "INVALID_UPLOAD_METADATA");
    return { filename, size, role: role, id: id, count, bytes, mime };
}
/** Register authenticated uploads beside the original MCP endpoint; identities and ownership stay unchanged. */
export function registerMaterialUploadRoutes(app, config, api, authorized) {
    const limits = limitsOf(config), origins = new Set([new URL(config.frontendUrl).origin, ...(config.publicAgentUrl ? [new URL(config.publicAgentUrl).origin] : [])]);
    const completed = new Map();
    const contexts = new WeakMap();
    const failure = (res, context, status, code) => {
        // Fixed response diagnostics only. The Router may discard process logs; no log lookup is promised.
        if (!res.headersSent && !res.destroyed)
            return res.status(status).json({ success: false, error: { code, request_id: context.requestId, stage: context.stage } });
    };
    app.use("/materials", (req, res, next) => {
        const context = { requestId: randomUUID(), stage: "access" };
        contexts.set(req, context);
        res.setHeader("X-Wuyu-Upload-Request-Id", context.requestId);
        const origin = req.header("origin");
        if (origin && !origins.has(origin))
            return failure(res, context, 403, "UPLOAD_ORIGIN_DENIED");
        if (origin)
            res.setHeader("Access-Control-Allow-Origin", origin);
        res.setHeader("Vary", "Origin");
        res.setHeader("Access-Control-Expose-Headers", "X-Wuyu-Upload-Request-Id");
        res.setHeader("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
        res.setHeader("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Project-Handle, X-Material-Role, X-Filename, X-File-Type, X-File-Size, X-Upload-Id, X-Batch-Count, X-Batch-Bytes");
        res.setHeader("Cache-Control", "no-store");
        if (req.method === "OPTIONS")
            return res.status(204).end();
        if (!authorized(req.header("authorization")))
            return failure(res, context, 401, "UNAUTHORIZED");
        next();
    });
    app.get("/materials/limits", (_req, res) => res.json({ success: true, limits }));
    app.post("/materials/upload", async (req, res) => {
        const context = contexts.get(req) || { requestId: randomUUID(), stage: "access" };
        let directory = "";
        let cleanupPromise;
        const cleanup = () => {
            if (!directory)
                return Promise.resolve();
            return cleanupPromise ??= (async () => {
                // Intake owns only this file and the empty mkdtemp directory.
                await unlink(join(directory, "content")).catch(() => { });
                await rmdir(directory).catch(() => { });
            })();
        };
        const respond = async (body, status = 200) => {
            await cleanup();
            if (!res.headersSent && !res.destroyed)
                res.status(status).json(body);
        };
        const abort = new AbortController(), disconnect = () => {
            if (!res.writableEnded)
                abort.abort();
        };
        req.once("aborted", disconnect);
        res.once("close", disconnect);
        const signal = AbortSignal.any([abort.signal, AbortSignal.timeout(config.materialUploadTimeoutMs ?? 180000)]);
        try {
            context.stage = "metadata";
            const file = metadata(req, limits);
            context.stage = "project";
            let projectId;
            try {
                projectId = projectIdFromHandle(req.header("x-project-handle") || "", config.capabilitySecret);
            }
            catch {
                throw new UploadFailure(403, "PROJECT_CONTEXT_DENIED");
            }
            // Prove the original backend service-account owner before accepting a large body.
            const before = record((await api.get(`/api/projects/${projectId}/workflow`, { signal })).data);
            if (before.project_id !== projectId || !Array.isArray(before.materials))
                throw new UploadFailure(403, "PROJECT_CONTEXT_DENIED");
            const projectLimit = record(before.material_limits), projectUsage = record(before.material_usage);
            const reusedUpload = before.materials.some(m => record(m).upload_id === file.id);
            if (!reusedUpload && ((positive(projectLimit.max_project_bytes) && Number(projectUsage.bytes || 0) + file.size > Number(projectLimit.max_project_bytes)) ||
                before.materials.filter(m => record(m).origin !== "web_research").length >= Number(projectLimit.max_project_files || limits.max_project_files)))
                throw new UploadFailure(413, "MATERIAL_PROJECT_LIMIT");
            if (Array.isArray(before.material_deletions) && before.material_deletions.length)
                throw new UploadFailure(409, "MATERIAL_DELETE_PENDING");
            context.stage = "receive";
            directory = await mkdtemp(join(tmpdir(), "wuyu-material-upload-"));
            const local = join(directory, "content"), hash = createHash("sha256");
            let received = 0;
            await pipeline(req, new Transform({ transform(chunk, _encoding, callback) {
                    received += chunk.length;
                    if (received > file.size || received > limits.max_file_bytes)
                        callback(new UploadFailure(413, "MATERIAL_UPLOAD_LIMIT"));
                    else {
                        hash.update(chunk);
                        callback(null, chunk);
                    }
                } }), createWriteStream(local, { flags: "wx", mode: 0o600 }), { signal });
            if (received !== file.size)
                throw new UploadFailure(400, "UPLOAD_LENGTH_MISMATCH");
            const sha = hash.digest("hex"), key = projectId + ":" + file.id, fingerprint = JSON.stringify([file.filename, file.role, file.size, file.mime, sha]);
            const prior = completed.get(key);
            if (prior && prior.fingerprint !== fingerprint)
                throw new UploadFailure(409, "UPLOAD_ID_CONFLICT");
            if (prior)
                return await respond({ ...await prior.promise, duplicate: true });
            context.stage = "register";
            const promise = (async () => {
                const response = await serializedRegistration(projectId, () => signal.aborted ? Promise.reject(new UploadFailure(408, abort.signal.aborted ? "UPLOAD_ABORTED" : "UPLOAD_TIMEOUT")) : api.postStreamingForm(`/api/projects/${projectId}/workflow/materials`, { role: file.role, upload_id: file.id, content_sha256: sha, batch_count: String(file.count), batch_bytes: String(file.bytes) }, { path: local, name: file.filename, size: file.size, type: file.mime }, { signal, timeoutMs: config.materialUploadTimeoutMs ?? 180000, headers: { "X-Upload-Id": file.id, "X-Upload-Sha256": sha, "X-Batch-Count": String(file.count), "X-Batch-Bytes": String(file.bytes) } }));
                context.stage = "result";
                const snapshot = record(response.data), old = new Set(before.materials.map(m => record(m).id));
                const ids = Array.isArray(snapshot.uploaded_material_ids) ? snapshot.uploaded_material_ids.filter(v => typeof v === "string") : (Array.isArray(snapshot.materials) ? snapshot.materials : []).map(record).filter(m => !old.has(m.id)).map(m => m.id).filter(v => typeof v === "string");
                if (!ids.length)
                    throw new UploadFailure(502, "UPLOAD_RESULT_UNAVAILABLE");
                return { success: true, project_id: projectId, revision: snapshot.revision, uploaded_material_ids: ids, received_bytes: received, sha256: sha, duplicate: false };
            })();
            completed.set(key, { fingerprint, promise });
            if (completed.size > 512)
                completed.delete(completed.keys().next().value);
            try {
                return await respond(await promise);
            }
            catch (error) {
                completed.delete(key);
                throw error;
            }
        }
        catch (error) {
            let status = error instanceof UploadFailure ? error.status : error instanceof BananaApiError ? error.status : signal.aborted ? 408 : 502;
            if (!Number.isInteger(status) || status < 400 || status > 599)
                status = 502;
            const upstream = error instanceof BananaApiError ? record(record(error.payload).error).code : undefined;
            // Never relay arbitrary upstream codes/messages. Status fallbacks stay useful for unknown refusals.
            const allowed = ["RELAYOUT_SOURCE_INVALID", "MATERIAL_UPLOAD_LIMIT", "MATERIAL_COUNT_LIMIT", "MATERIAL_BATCH_LIMIT", "MATERIAL_PROJECT_LIMIT", "MATERIAL_DELETE_PENDING", "MATERIAL_HASH_MISMATCH", "UPLOAD_ID_CONFLICT", "INVALID_UPLOAD_METADATA", "UPLOAD_LENGTH_MISMATCH", "UPLOAD_TYPE_UNSUPPORTED", "BINARY_FILE_REQUIRED", "PROJECT_CONTEXT_DENIED", "UNAUTHORIZED"];
            const fallback = { 400: "UPLOAD_REQUEST_REJECTED", 401: "UNAUTHORIZED", 403: "UPLOAD_ACCESS_DENIED", 404: "UPLOAD_TARGET_UNAVAILABLE", 408: "UPLOAD_TIMEOUT", 409: "UPLOAD_STATE_CONFLICT", 413: "UPLOAD_REQUEST_TOO_LARGE", 415: "BINARY_FILE_REQUIRED", 422: "UPLOAD_REQUEST_REJECTED", 429: "UPLOAD_RATE_LIMITED" };
            const code = error instanceof UploadFailure ? error.code : typeof upstream === "string" && allowed.includes(upstream) ? upstream : signal.aborted ? abort.signal.aborted ? "UPLOAD_ABORTED" : "UPLOAD_TIMEOUT" : fallback[status] || (status < 500 ? "UPLOAD_REQUEST_REJECTED" : "UPLOAD_SERVICE_UNAVAILABLE");
            await cleanup();
            failure(res, context, status, code);
        }
        finally {
            req.removeListener("aborted", disconnect);
            res.removeListener("close", disconnect);
            await cleanup();
        }
    });
}
