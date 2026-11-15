/** React-free per-session upload queue; only metadata enters the display snapshot. */
import type { SnapshotStore } from '@deepseek-ai/dsh-client-store';
import type { MaterialBinding, MaterialRole } from './material-upload.ts';
import { type MaterialLimits, type BinaryUploadResult } from './material-http.ts';
export type UploadPhase = 'queued' | 'uploading' | 'registering' | 'uploaded' | 'attached' | 'failed' | 'paused';
export interface UploadRow {
    id: string;
    filename: string;
    size: number;
    role: MaterialRole;
    phase: UploadPhase;
    percent: number;
    error: string | null;
}
export interface UploadSnapshot {
    rows: readonly UploadRow[];
    limits: MaterialLimits | null;
    busy: boolean;
    syncing: boolean;
    syncFailed: boolean;
    intakeError: string | null;
}
export declare const initialUploadSnapshot: () => UploadSnapshot;
interface Project {
    handle: string;
    id?: string;
    materialCount: number;
}
interface QueueDeps {
    limits(): Promise<MaterialLimits>;
    project(handle: string | null): Promise<Project>;
    upload(file: File, input: {
        projectHandle: string;
        role: MaterialRole;
        uploadId: string;
        batchCount: number;
        batchBytes: number;
    }, signal: AbortSignal, progress: (percent: number) => void): Promise<BinaryUploadResult>;
    bind(binding: MaterialBinding): Promise<void>;
    ready(project: Project): void;
}
/** Partition a large selection into bounded requests without dropping its later files. */
export declare function materialBatches(files: readonly File[], limits: MaterialLimits): File[][];
/** File references and cancellation stay in this controller, outside logged messages and stores. */
export declare class MaterialUploadQueue {
    readonly state: SnapshotStore<UploadSnapshot>;
    private readonly deps;
    private readonly files;
    private readonly projects;
    private readonly scopes;
    private readonly bindings;
    private intake;
    private running;
    private disposed;
    private active;
    constructor(state: SnapshotStore<UploadSnapshot>, deps: QueueDeps);
    private set;
    private row;
    /** Append a user-selected FileList, including selections made during another upload. */
    add(input: ArrayLike<File>, role: MaterialRole, handle: string | null): Promise<void>;
    private scope;
    private resolveProject;
    private run;
    /** Retry synchronization only; already accepted bytes are never sent again. */
    sync(): Promise<void>;
    /** Retry failed transfers in place, retaining their idempotency identity. */
    retry(id?: string): void;
    pause(id: string): void;
    remove(id: string): void;
    dispose(): void;
}
export {};
//# sourceMappingURL=material-queue.d.ts.map