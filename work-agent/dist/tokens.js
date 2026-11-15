import { createHmac, timingSafeEqual } from "node:crypto";
function encode(value) {
    return Buffer.from(value, "utf8").toString("base64url");
}
function sign(encodedPayload, secret) {
    return createHmac("sha256", secret).update(encodedPayload).digest("base64url");
}
function issue(payload, secret) {
    const encodedPayload = encode(JSON.stringify(payload));
    return `${encodedPayload}.${sign(encodedPayload, secret)}`;
}
function verify(token, secret) {
    if (token.length > 8192)
        throw new Error("Invalid capability token");
    const [encodedPayload, providedSignature, extra] = token.split(".");
    if (!encodedPayload || !providedSignature || extra) {
        throw new Error("Invalid capability token");
    }
    const expected = Buffer.from(sign(encodedPayload, secret));
    const provided = Buffer.from(providedSignature);
    if (expected.length !== provided.length || !timingSafeEqual(expected, provided)) {
        throw new Error("Invalid capability token");
    }
    try {
        const payload = JSON.parse(Buffer.from(encodedPayload, "base64url").toString("utf8"));
        if (!payload || typeof payload !== "object")
            throw new Error();
        return payload;
    }
    catch {
        throw new Error("Invalid capability token");
    }
}
export function issueProjectHandle(projectId, secret) {
    return issue({ kind: "project", projectId, issuedAt: Date.now() }, secret);
}
export function projectIdFromHandle(handle, secret) {
    const payload = verify(handle, secret);
    if (payload.kind !== "project" || typeof payload.projectId !== "string" || !/^[\w-]{1,128}$/.test(payload.projectId)) {
        throw new Error("Invalid project handle");
    }
    return payload.projectId;
}
/** op181: project artifact links use a compact binary form (a1.<payload>.<128-bit HMAC>) that a chat model can copy reliably. */
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const toUuid = (bytes) => { const x = bytes.toString("hex"); return `${x.slice(0, 8)}-${x.slice(8, 12)}-${x.slice(12, 16)}-${x.slice(16, 20)}-${x.slice(20)}`; };
const compactSign = (body, secret) => createHmac("sha256", secret).update("a1." + body).digest().subarray(0, 16).toString("base64url");
function compactAssetToken(path, projectId, expiresAt, secret) {
    const prefix = `/api/projects/${projectId}/workflow/artifacts/`;
    if (typeof projectId !== "string" || !UUID.test(projectId) || !path.startsWith(prefix))
        return null;
    const match = /^([0-9a-f-]{36})(\?download=1)?$/i.exec(path.slice(prefix.length));
    if (!match || !UUID.test(match[1]))
        return null;
    const expiry = Buffer.alloc(4);
    expiry.writeUInt32BE(Math.floor(expiresAt / 1000));
    const body = Buffer.concat([Buffer.from(projectId.replace(/-/g, ""), "hex"), Buffer.from(match[1].replace(/-/g, ""), "hex"), Buffer.from([match[2] ? 1 : 0]), expiry]).toString("base64url");
    return `a1.${body}.${compactSign(body, secret)}`;
}
function verifyCompactAssetToken(token, secret) {
    const [tag, body, signature, extra] = token.split(".");
    if (tag !== "a1" || !body || !signature || extra !== undefined || token.length > 200)
        throw new Error("Invalid or expired asset capability");
    const expected = Buffer.from(compactSign(body, secret)), provided = Buffer.from(signature);
    if (expected.length !== provided.length || !timingSafeEqual(expected, provided))
        throw new Error("Invalid or expired asset capability");
    const bytes = Buffer.from(body, "base64url");
    if (bytes.length !== 37 || bytes[32] > 1)
        throw new Error("Invalid or expired asset capability");
    const projectId = toUuid(bytes.subarray(0, 16));
    return { kind: "asset", scope: "project", projectId, expiresAt: bytes.readUInt32BE(33) * 1000,
        path: `/api/projects/${projectId}/workflow/artifacts/${toUuid(bytes.subarray(16, 32))}${bytes[32] ? "?download=1" : ""}` };
}
/** Short-lived, read-only capability. It never grants MCP or backend API access. */
export function issueAssetToken(scope, path, secret, projectId) {
    const expiresAt = Date.now() + 24 * 60 * 60 * 1000;
    if (scope === "project") {
        const compact = compactAssetToken(path, projectId, expiresAt, secret);
        if (compact)
            return compact;
    }
    return issue({ kind: "asset", scope, path, projectId, expiresAt }, secret);
}
export function verifyAssetToken(token, secret) {
    const payload = typeof token === "string" && token.startsWith("a1.") ? verifyCompactAssetToken(token, secret) : verify(token, secret);
    if (payload.kind !== "asset" || !["library", "project"].includes(payload.scope) ||
        typeof payload.path !== "string" || !Number.isFinite(payload.expiresAt) || payload.expiresAt <= Date.now() ||
        (payload.scope === "project" && (typeof payload.projectId !== "string" || !/^[\w-]{1,128}$/.test(payload.projectId)))) {
        throw new Error("Invalid or expired asset capability");
    }
    return payload;
}
export function issueExportApproval(projectId, revision, secret) {
    return issue({ kind: "export", projectId, revision, issuedAt: Date.now() }, secret);
}
export function verifyExportApproval(token, projectId, revision, secret) {
    const payload = verify(token, secret);
    if (payload.kind !== "export" || payload.projectId !== projectId || payload.revision !== revision) {
        throw new Error("Slide approval is missing or stale; ask the user to confirm the current slides again");
    }
}
