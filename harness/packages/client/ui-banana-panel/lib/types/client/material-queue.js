import { MATERIAL_ACCEPT } from "./material-upload.js";
import { MaterialUploadError } from "./material-http.js";
export const initialUploadSnapshot = () => ({ rows: [], limits: null, busy: false, syncing: false, syncFailed: false, intakeError: null });
/** Partition a large selection into bounded requests without dropping its later files. */
export function materialBatches(files, limits) {
    const batches = [];
    let current = [], bytes = 0;
    for (const file of files) {
        if (current.length && (current.length >= limits.max_batch_files || bytes + file.size > limits.max_batch_bytes)) {
            batches.push(current);
            current = [];
            bytes = 0;
        }
        ;
        current.push(file);
        bytes += file.size;
    }
    if (current.length)
        batches.push(current);
    return batches;
}
/** File references and cancellation stay in this controller, outside logged messages and stores. */
export class MaterialUploadQueue {
    state;
    deps;
    files = new Map();
    projects = new Map();
    scopes = new Map();
    bindings = [];
    intake = Promise.resolve();
    running = false;
    disposed = false;
    active = null;
    constructor(state, deps) {
        this.state = state;
        this.deps = deps;
    }
    set(patch) { this.state.set({ ...this.state.getSnapshot(), ...patch }); }
    row(id, patch) { this.set({ rows: this.state.getSnapshot().rows.map(row => row.id === id ? { ...row, ...patch } : row) }); }
    /** Append a user-selected FileList, including selections made during another upload. */
    add(input, role, handle) {
        const chosen = Array.from(input);
        this.intake = this.intake.then(async () => {
            if (this.disposed || !chosen.length)
                return;
            let limits;
            try {
                limits = this.state.getSnapshot().limits || await this.deps.limits();
                this.set({ limits, intakeError: null });
            }
            catch {
                const rows = [...this.state.getSnapshot().rows];
                for (const file of chosen) {
                    const id = crypto.randomUUID();
                    this.files.set(id, { file, handle, batchCount: 1, batchBytes: file.size });
                    rows.push({ id, filename: file.name, size: file.size, role, phase: 'failed', percent: 0, error: 'UPLOAD_SERVICE_UNAVAILABLE' });
                }
                this.set({ rows, intakeError: 'UPLOAD_SERVICE_UNAVAILABLE' });
                return;
            }
            const rows = [...this.state.getSnapshot().rows], accepted = [], ids = new Map();
            for (const file of chosen) {
                // Metadata cannot establish equal file contents, including clipboard images.
                // Only the same File object submitted again to this scope/role is a duplicate.
                const duplicate = rows.some(row => {
                    const existing = this.files.get(row.id);
                    return existing?.file === file && row.role === role &&
                        this.scope(existing.handle) === this.scope(handle);
                });
                if (duplicate)
                    continue;
                // Failed and paused transfers do not reserve server material slots.
                // The backend remains authoritative, including ambiguous response-loss retries.
                const occupied = rows.filter(row => {
                    const existing = this.files.get(row.id);
                    return existing && this.scope(existing.handle) === this.scope(handle) &&
                        !['failed', 'paused'].includes(row.phase);
                }).length;
                const id = crypto.randomUUID();
                const extension = file.name.includes('.') ? '.' + file.name.split('.').at(-1).toLowerCase() : '';
                const error = !file.size ? 'UPLOAD_EMPTY' :
                    file.size > limits.max_file_bytes ? 'MATERIAL_UPLOAD_LIMIT' :
                        !MATERIAL_ACCEPT[role].split(',').includes(extension) ? 'UPLOAD_TYPE_UNSUPPORTED' :
                            occupied >= limits.max_project_files ? 'MATERIAL_COUNT_LIMIT' : null;
                this.files.set(id, { file, handle, batchCount: 1, batchBytes: file.size });
                rows.push({ id, filename: file.name, size: file.size, role, phase: error ? 'failed' : 'queued', percent: 0, error });
                if (!error) {
                    accepted.push(file);
                    ids.set(file, id);
                }
            }
            for (const batch of materialBatches(accepted, limits))
                for (const file of batch) {
                    const item = this.files.get(ids.get(file));
                    item.batchCount = batch.length;
                    item.batchBytes = batch.reduce((sum, file) => sum + file.size, 0);
                }
            this.set({ rows });
            void this.run();
        }).catch(() => { this.set({ intakeError: 'UPLOAD_SERVICE_UNAVAILABLE' }); });
        return this.intake;
    }
    scope(handle) { return handle ? this.scopes.get(handle) || handle : 'new'; }
    resolveProject(handle) {
        const key = this.scope(handle);
        let project = this.projects.get(key);
        if (!project) {
            project = this.deps.project(handle).then(value => { this.scopes.set(value.handle, key); return value; }).catch(error => { this.projects.delete(key); throw error; });
            this.projects.set(key, project);
        }
        return project;
    }
    async run() {
        if (this.running || this.disposed)
            return;
        this.running = true;
        this.set({ busy: true });
        try {
            if (!this.state.getSnapshot().limits) {
                try {
                    this.set({ limits: await this.deps.limits(), intakeError: null });
                }
                catch {
                    this.set({ rows: this.state.getSnapshot().rows.map(row => row.phase === 'queued' ? { ...row, phase: 'failed', error: 'UPLOAD_SERVICE_UNAVAILABLE' } : row) });
                    return;
                }
            }
            for (;;) {
                const row = this.state.getSnapshot().rows.find(row => row.phase === 'queued');
                if (!row || this.disposed)
                    break;
                const item = this.files.get(row.id), abort = new AbortController();
                this.active = { id: row.id, abort };
                try {
                    const limits = this.state.getSnapshot().limits, extension = '.' + item.file.name.split('.').at(-1).toLowerCase();
                    if (!item.file.size)
                        throw new MaterialUploadError('UPLOAD_EMPTY');
                    if (item.file.size > limits.max_file_bytes)
                        throw new MaterialUploadError('MATERIAL_UPLOAD_LIMIT');
                    if (!MATERIAL_ACCEPT[row.role].split(',').includes(extension))
                        throw new MaterialUploadError('UPLOAD_TYPE_UNSUPPORTED');
                    item.project = await this.resolveProject(item.handle);
                    // The backend checks durable upload identity before its project count limit.
                    // A lost response can therefore be retried even after the project reaches 100 files.
                    if (abort.signal.aborted)
                        throw new MaterialUploadError('UPLOAD_ABORTED');
                    this.row(row.id, { phase: 'uploading', error: null });
                    const result = await this.deps.upload(item.file, { projectHandle: item.project.handle, role: row.role, uploadId: row.id, batchCount: item.batchCount, batchBytes: item.batchBytes }, abort.signal, percent => this.row(row.id, { percent, phase: percent === 100 ? 'registering' : 'uploading' }));
                    item.ids = result.uploaded_material_ids;
                    item.project.materialCount += item.ids.length;
                    this.row(row.id, { phase: 'uploaded', percent: 100, error: null });
                }
                catch (error) {
                    const code = error instanceof MaterialUploadError ? error.code : 'UPLOAD_SERVICE_UNAVAILABLE';
                    this.row(row.id, { phase: code === 'UPLOAD_ABORTED' ? 'paused' : 'failed', error: code });
                }
                finally {
                    this.active = null;
                }
            }
            const pending = this.state.getSnapshot().rows.filter(row => row.phase === 'uploaded' && !this.bindings.some(binding => binding.rows.includes(row.id)));
            const groups = new Map();
            for (const row of pending) {
                const file = this.files.get(row.id);
                if (!file.project || !file.ids?.length)
                    continue;
                const group = groups.get(file.project.handle) || { rows: [], ids: [], project: file.project };
                group.rows.push(row.id);
                group.ids.push(...file.ids);
                groups.set(file.project.handle, group);
            }
            for (const group of groups.values())
                this.bindings.push({ request: { projectHandle: group.project.handle, materialIds: [...new Set(group.ids)], requestId: crypto.randomUUID() }, rows: group.rows, project: group.project });
            await this.sync();
        }
        finally {
            this.running = false;
            this.set({ busy: false });
            if (this.state.getSnapshot().rows.some(row => row.phase === 'queued'))
                void this.run();
        }
    }
    /** Retry synchronization only; already accepted bytes are never sent again. */
    async sync() {
        if (this.state.getSnapshot().syncing || this.disposed)
            return;
        this.set({ syncing: true, syncFailed: false });
        try {
            while (this.bindings.length) {
                const current = this.bindings[0];
                await this.deps.bind(current.request);
                for (const id of current.rows)
                    this.row(id, { phase: 'attached' });
                this.bindings.shift();
                this.deps.ready(current.project);
            }
        }
        catch {
            this.set({ syncFailed: true });
        }
        finally {
            this.set({ syncing: false });
        }
    }
    /** Retry failed transfers in place, retaining their idempotency identity. */
    retry(id) { this.set({ rows: this.state.getSnapshot().rows.map(row => (!id || row.id === id) && ['failed', 'paused'].includes(row.phase) ? { ...row, phase: 'queued', error: null, percent: 0 } : row) }); void this.run(); }
    pause(id) {
        if (this.active?.id === id)
            this.active.abort.abort();
        else
            this.row(id, { phase: 'paused' });
    }
    remove(id) {
        const row = this.state.getSnapshot().rows.find(row => row.id === id);
        if (!row || ['uploading', 'registering'].includes(row.phase) || this.bindings.some(binding => binding.rows.includes(id)))
            return;
        if (this.active?.id === id)
            this.active.abort.abort();
        this.files.delete(id);
        this.set({ rows: this.state.getSnapshot().rows.filter(row => row.id !== id) });
    }
    dispose() { this.disposed = true; this.active?.abort.abort(); this.files.clear(); this.bindings.length = 0; }
}
//# sourceMappingURL=material-queue.js.map