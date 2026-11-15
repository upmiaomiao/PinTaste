import { backendFetch } from "./backend-http.js";
import { createReadStream } from "node:fs";
import { readFile } from "node:fs/promises";
import { randomBytes } from "node:crypto";
import { Readable } from "node:stream";
export class BananaApiError extends Error {
    status;
    payload;
    constructor(message, status, payload) {
        super(message);
        this.status = status;
        this.payload = payload;
    }
}
export class BananaApiClient {
    config;
    bearerToken = "";
    loginPromise;
    constructor(config) {
        this.config = config;
        const url = new URL(config.baseUrl);
        if (!["http:", "https:"].includes(url.protocol) || url.username || url.password || url.search || url.hash) {
            throw new BananaApiError("Invalid backend service URL", 400);
        }
    }
    async boundedText(response, limit = 32 * 1024 * 1024) {
        const declared = Number(response.headers.get("content-length") || 0);
        if (declared > limit) {
            await response.body?.cancel();
            throw new BananaApiError("Backend response exceeds size limit", 413);
        }
        const reader = response.body?.getReader();
        if (!reader)
            return "";
        const chunks = [];
        let length = 0;
        while (true) {
            const { done, value } = await reader.read();
            if (done)
                break;
            length += value.length;
            if (length > limit) {
                await reader.cancel();
                throw new BananaApiError("Backend response exceeds size limit", 413);
            }
            chunks.push(value);
        }
        return Buffer.concat(chunks).toString("utf8");
    }
    async loginOnce() {
        if (this.config.actorTokenPath) {
            let token;
            try {
                token = (await readFile(this.config.actorTokenPath, "utf8")).trim();
            }
            catch {
                throw new BananaApiError("Account authentication is unavailable", 503);
            }
            if (token.length > 16384 || !/^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/.test(token)) {
                throw new BananaApiError("Account authentication is unavailable", 503);
            }
            this.bearerToken = token;
            return;
        }
        const response = await backendFetch(`${this.config.baseUrl}/api/auth/login`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                username: this.config.username,
                password: this.config.password,
            }),
            redirect: "error",
            signal: AbortSignal.timeout(this.config.timeoutMs ?? 90000),
        });
        let payload = null;
        try {
            payload = JSON.parse(await this.boundedText(response, 128 * 1024));
        }
        catch (error) {
            if (error instanceof BananaApiError)
                throw error;
        }
        const data = payload?.data;
        const token = data?.token;
        if (!response.ok || typeof token !== "string" || !token.trim() || token.length > 16384) {
            throw new BananaApiError("Banana Slides service account login failed", response.status, payload);
        }
        this.bearerToken = token;
    }
    async login() {
        if (!this.loginPromise)
            this.loginPromise = this.loginOnce().finally(() => { this.loginPromise = undefined; });
        return this.loginPromise;
    }
    validatePath(path) {
        if (!path.startsWith("/") || path.startsWith("//") || path.includes("\\") || /[\r\n]/.test(path)) {
            throw new BananaApiError("Invalid backend request path", 400);
        }
    }
    async request(path, init = {}, retryAuth = true) {
        this.validatePath(path);
        init.signal?.throwIfAborted();
        if (!this.bearerToken) {
            // Cancelling one read must not cancel the coalesced login other calls use.
            // Its caller still stops immediately while that shared login settles.
            if (init.signal) {
                const signal = init.signal;
                await new Promise((resolve, reject) => {
                    const abort = () => reject(signal.reason || new Error("Request cancelled"));
                    signal.addEventListener("abort", abort, { once: true });
                    this.login().then(resolve, reject).finally(() => signal.removeEventListener("abort", abort));
                });
            }
            else
                await this.login();
        }
        init.signal?.throwIfAborted();
        const headers = new Headers(init.headers);
        headers.set("Authorization", `Bearer ${this.bearerToken}`);
        if (init.body && !(init.body instanceof FormData)) {
            headers.set("Content-Type", "application/json");
        }
        const usedToken = this.bearerToken;
        const response = await backendFetch(`${this.config.baseUrl}${path}`, { ...init, headers,
            redirect: "error", signal: init.signal ? AbortSignal.any([init.signal, AbortSignal.timeout(this.config.timeoutMs ?? 90000)]) : AbortSignal.timeout(this.config.timeoutMs ?? 90000) });
        if (response.status === 401 && retryAuth) {
            await response.body?.cancel();
            if (this.bearerToken === usedToken)
                this.bearerToken = "";
            return this.request(path, init, false);
        }
        const contentType = response.headers.get("content-type") ?? "";
        const text = await this.boundedText(response);
        let payload = text;
        if (contentType.includes("application/json")) {
            try {
                payload = JSON.parse(text);
            }
            catch {
                throw new BananaApiError("Invalid backend JSON response", 502);
            }
        }
        if (!response.ok) {
            const apiError = payload && typeof payload === "object"
                ? payload.error
                : undefined;
            throw new BananaApiError(typeof apiError?.message === "string" ? apiError.message : `Banana Slides API returned ${response.status}`, response.status, payload);
        }
        return payload;
    }
    /** Fetch a private artifact with the service account, without exposing its JWT. */
    async getBinaryStream(path, signal, range, retryAuth = true) {
        this.validatePath(path);
        if (range && !/^bytes=\d*-\d*$/.test(range))
            throw new BananaApiError("Invalid byte range", 416);
        if (!this.bearerToken)
            await this.login();
        const usedToken = this.bearerToken;
        const response = await backendFetch(`${this.config.baseUrl}${path}`, {
            headers: { Authorization: `Bearer ${usedToken}`, ...(range ? { Range: range } : {}) },
            redirect: "error", signal: AbortSignal.any([signal, AbortSignal.timeout(this.config.timeoutMs ?? 90000)]),
        });
        if (response.status === 401 && retryAuth) {
            await response.body?.cancel();
            if (this.bearerToken === usedToken)
                this.bearerToken = "";
            return this.getBinaryStream(path, signal, range, false);
        }
        if (!response.ok || Number(response.headers.get("content-length") || 0) > 200 * 1024 * 1024) {
            await response.body?.cancel();
            throw new BananaApiError("Artifact is unavailable", response.ok ? 413 : response.status);
        }
        return response;
    }
    /** Fetch a bounded artifact for consumers that require all bytes. */
    async getBinary(path, retryAuth = true) {
        this.validatePath(path);
        if (!this.bearerToken)
            await this.login();
        const usedToken = this.bearerToken;
        const response = await backendFetch(`${this.config.baseUrl}${path}`, {
            headers: { Authorization: `Bearer ${usedToken}` }, redirect: "error",
            signal: AbortSignal.timeout(this.config.timeoutMs ?? 90000),
        });
        if (response.status === 401 && retryAuth) {
            await response.body?.cancel();
            if (this.bearerToken === usedToken)
                this.bearerToken = "";
            return this.getBinary(path, false);
        }
        if (!response.ok) {
            await response.body?.cancel();
            throw new BananaApiError(`Artifact request returned ${response.status}`, response.status);
        }
        const declared = Number(response.headers.get("content-length") || 0);
        const limit = 200 * 1024 * 1024;
        if (declared > limit) {
            await response.body?.cancel();
            throw new BananaApiError("Artifact exceeds 200 MB", 413);
        }
        const reader = response.body?.getReader();
        const chunks = [];
        let length = 0;
        if (reader)
            while (true) {
                const { done, value } = await reader.read();
                if (done)
                    break;
                length += value.length;
                if (length > limit) {
                    await reader.cancel();
                    throw new BananaApiError("Artifact exceeds 200 MB", 413);
                }
                chunks.push(value);
            }
        return { data: Buffer.concat(chunks), contentType: response.headers.get("content-type") || "application/octet-stream",
            disposition: response.headers.get("content-disposition") };
    }
    get(path, options = {}) {
        return this.request(path, options);
    }
    post(path, body) {
        return this.request(path, {
            method: "POST",
            body: body === undefined ? undefined : JSON.stringify(body),
        });
    }
    /** POST a multipart/form-data body with one file field (template upload). */
    postForm(path, fields, fileField, file) {
        const form = new FormData();
        for (const [key, value] of Object.entries(fields)) {
            form.append(key, value);
        }
        const arrayBuffer = file.data.buffer.slice(file.data.byteOffset, file.data.byteOffset + file.data.byteLength);
        form.append(fileField, new Blob([arrayBuffer], { type: file.type }), file.name);
        return this.request(path, { method: "POST", body: form });
    }
    /** Stream one controlled temporary file as multipart; reopen it for one auth retry. */
    async postStreamingForm(path, fields, file, options = {}, retryAuth = true) {
        this.validatePath(path);
        const signal = options.signal ? AbortSignal.any([options.signal, AbortSignal.timeout(options.timeoutMs ?? 180000)]) : AbortSignal.timeout(options.timeoutMs ?? 180000);
        signal.throwIfAborted();
        if (!this.bearerToken) {
            await new Promise((resolve, reject) => {
                const abort = () => reject(signal.reason);
                signal.addEventListener("abort", abort, { once: true });
                this.login().then(resolve, reject).finally(() => signal.removeEventListener("abort", abort));
            });
        }
        signal.throwIfAborted();
        const boundary = "wuyu-" + randomBytes(18).toString("hex");
        const quoted = (value) => value.replace(/[\r\n]/g, "_").replace(/["\\]/g, "\\$&");
        const prefix = Buffer.from(Object.entries(fields).map(([key, value]) => `--${boundary}\r\nContent-Disposition: form-data; name="${quoted(key)}"\r\n\r\n${value}\r\n`).join("") +
            `--${boundary}\r\nContent-Disposition: form-data; name="file"; filename="${quoted(file.name)}"\r\nContent-Type: ${file.type.replace(/[\r\n]/g, "")}\r\n\r\n`);
        const suffix = Buffer.from(`\r\n--${boundary}--\r\n`);
        async function* multipart() { yield prefix; for await (const chunk of createReadStream(file.path))
            yield chunk; yield suffix; }
        const stream = Readable.from(multipart());
        const usedToken = this.bearerToken;
        let response;
        try {
            const init = { method: "POST", redirect: "error", signal, duplex: "half",
                headers: { ...options.headers, Authorization: `Bearer ${usedToken}`, "Content-Type": `multipart/form-data; boundary=${boundary}`, "Content-Length": String(prefix.length + file.size + suffix.length) },
                body: Readable.toWeb(stream) };
            response = await backendFetch(`${this.config.baseUrl}${path}`, init);
        if (response.status === 401 && retryAuth) {
            await response.body?.cancel();
            if (this.bearerToken === usedToken)
                this.bearerToken = "";
            return this.postStreamingForm(path, fields, file, { ...options, signal }, false);
        }
        let payload;
        try {
            payload = JSON.parse(await this.boundedText(response));
        }
        catch (error) {
            if (error instanceof BananaApiError)
                throw error;
            throw new BananaApiError("Invalid backend JSON response", 502);
        }
        if (!response.ok)
            throw new BananaApiError(`Material upload returned ${response.status}`, response.status, payload);
        return payload;
        } finally {
            stream.destroy();
        }
    }
    put(path, body) {
        return this.request(path, { method: "PUT", body: JSON.stringify(body) });
    }
    delete(path) {
        return this.request(path, { method: "DELETE" });
    }
}
