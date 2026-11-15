/** Build a named Markdown link while preserving the actual artifact URL byte for byte. */
export function namedDeliveryLink(url, label, filename) {
    if (typeof url !== "string" || /[\s<>\x00-\x1f]/.test(url))
        return undefined;
    let destination;
    try {
        destination = new URL(url);
    }
    catch {
        return undefined;
    }
    if (!["http:", "https:"].includes(destination.protocol) || destination.username || destination.password)
        return undefined;
    const clean = (value) => {
        if (typeof value !== "string")
            return undefined;
        const text = value.trim().replace(/\s+/g, " ");
        if (!text || text.length > 180 || /https?:\/\/|token=|\bBearer\b|\bsk-[\w-]+|[\\/\x00-\x1f]/i.test(text))
            return undefined;
        return text.replace(/[\\\[\]<>`*_]/g, "\\$&");
    };
    const name = clean(filename), title = clean(label) || "查看文件";
    return `[${title}${name && name !== title ? ` · ${name}` : ""}](<${url}>)`;
}
