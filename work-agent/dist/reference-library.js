import { readFile, realpath, stat } from "node:fs/promises";
import { dirname, extname, isAbsolute, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { issueAssetToken } from "./tokens.js";
export const TAG_AXES = ["purpose", "page_role", "visual_style", "information_density", "layout_structure", "industry_topic"];
export function assetUrl(scope, path, config, projectId, filename) {
    const base = (config.publicAgentUrl || process.env.BANANA_PUBLIC_AGENT_URL || "http://127.0.0.1:3012").replace(/\/$/, "");
    const extension = filename?.match(/\.(pptx|pdf|zip)$/i)?.[0].toLowerCase();
    const suffix = path.endsWith('?download=1') && extension ? `/wuyu-report${extension}` : '';
    return `${base}/assets${suffix}?token=${encodeURIComponent(issueAssetToken(scope, path, config.capabilitySecret, projectId))}`;
}
/** Read the accepted library in place. Only catalog-declared raster previews are served. */
export class ReferenceLibrary {
    root;
    catalogPath;
    loaded;
    allowedImages = new Set();
    constructor(root = process.env.WUYU_TEMPLATE_LIBRARY_PATH || resolve(dirname(fileURLToPath(import.meta.url)), "../../../../template_library")) {
        this.catalogPath = extname(root).toLowerCase() === ".json" ? resolve(root) : resolve(root, "catalog.json");
        this.root = dirname(this.catalogPath);
    }
    async catalog() {
        if (!this.loaded)
            this.loaded = (async () => {
                const parsed = JSON.parse(await readFile(this.catalogPath, "utf8"));
                if (!Array.isArray(parsed.sources))
                    throw new Error("Template catalog is invalid");
                const sources = parsed.sources;
                for (const source of sources) {
                    for (const path of [source.cover, ...(source.pages || []).map(p => p.image)]) {
                        if (typeof path === "string" && /\.(png|jpe?g|webp|gif)$/i.test(path) && !path.includes("..") && !path.includes(":"))
                            this.allowedImages.add(path);
                    }
                }
                return { sources, facets: parsed.facets || {} };
            })().catch(error => { this.loaded = undefined; throw error; });
        return this.loaded;
    }
    present(source, config, detail = false) {
        const result = {};
        for (const key of ["id", "title", "source_type", "format", "summary", "tags", "page_count", "source_url", "review_status"])
            result[key] = source[key];
        result.cover_url = typeof source.cover === "string" && this.allowedImages.has(source.cover) ? assetUrl("library", source.cover, config) : null;
        result.cover = result.cover_url;
        if (detail)
            result.pages = (source.pages || []).map(page => {
                const out = {};
                for (const key of ["id", "source_id", "page_number", "title", "tags", "text_excerpt", "object_counts", "preview_kind", "review_status", "editability"])
                    out[key] = page[key];
                out.image_url = typeof page.image === "string" && this.allowedImages.has(page.image) ? assetUrl("library", page.image, config) : null;
                out.image = out.image_url;
                return out;
            });
        return result;
    }
    async list(filters, config) {
        const { sources, facets } = await this.catalog();
        const terms = String(filters.search || "").toLocaleLowerCase().split(/\s+/).filter(Boolean);
        const selected = sources.filter(source => {
            if (filters.source_type && filters.source_type !== source.source_type)
                return false;
            for (const axis of TAG_AXES)
                if (filters[axis] && !(source.tags?.[axis] || []).includes(String(filters[axis])))
                    return false;
            const text = [source.title, source.summary, ...Object.values(source.tags || {}).flat(), ...(source.pages || []).map(p => `${p.title || ""} ${p.text_excerpt || ""}`)].join(" ").toLocaleLowerCase();
            return terms.every(term => text.includes(term));
        });
        const offset = Math.max(0, Math.floor(Number(filters.offset) || 0));
        const limit = Math.max(1, Math.min(50, Math.floor(Number(filters.limit) || 12)));
        return { items: selected.slice(offset, offset + limit).map(s => this.present(s, config)), total: selected.length, offset, limit, facets };
    }
    async detail(id, config) {
        const { sources } = await this.catalog();
        const source = sources.find(s => s.id === id);
        if (!source)
            throw new Error("Template reference was not found");
        return this.present(source, config, true);
    }
    async image(path) {
        await this.catalog();
        if (!this.allowedImages.has(path))
            throw new Error("Unknown template preview");
        const root = await realpath(this.root);
        const file = await realpath(resolve(root, path));
        const rel = relative(root, file);
        if (isAbsolute(rel) || rel.startsWith(`..${sep}`) || rel === ".." || resolve(file) === root)
            throw new Error("Invalid preview path");
        if ((await stat(file)).size > 30 * 1024 * 1024)
            throw new Error("Preview is too large");
        const contentType = ({ ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif" })[extname(file).toLowerCase()] || "application/octet-stream";
        return { data: await readFile(file), contentType };
    }
}
export const referenceLibrary = new ReferenceLibrary();
