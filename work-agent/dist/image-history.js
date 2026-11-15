/** Add UI-only page-image record labels before private artifact URLs are signed. */
export function withImageHistory(snapshot, projectId) {
    if (!Array.isArray(snapshot.artifacts)) return snapshot;
    const pages = Array.isArray(snapshot.workflow?.pages) ? snapshot.workflow.pages : [];
    const counters = new Map(), seen = new Set();
    const artifacts = snapshot.artifacts.map(original => {
        if (!original || typeof original !== 'object') return original;
        const metadata = original.metadata && typeof original.metadata === 'object' && !Array.isArray(original.metadata) ? original.metadata : {};
        const { image_history: _untrusted, ...rest } = metadata;
        const artifact = Object.hasOwn(metadata, 'image_history') ? { ...original, metadata: rest } : original;
        if (!artifact || typeof artifact !== 'object') return artifact;
        const number = artifact.metadata?.number;
        if (snapshot.project_id !== projectId || !['slide_image','sample_image'].includes(artifact.kind) || typeof artifact.id !== 'string' || !/^[\w-]{1,128}$/.test(artifact.id)
            || !Number.isInteger(number) || number < 1 || number > 80 || seen.has(artifact.id)
            || artifact.url !== `/api/projects/${projectId}/workflow/artifacts/${artifact.id}`) return artifact;
        seen.add(artifact.id);
        const record = (counters.get(number) || 0) + 1;
        counters.set(number, record);
        return { ...artifact, metadata: { ...artifact.metadata, image_history: {
            project_id: projectId, page_number: number, record_number: record,
            is_current: pages.some(page => page.number === number && page.artifact?.image_uri === artifact.url),
        } } };
    });
    return { ...snapshot, artifacts };
}
