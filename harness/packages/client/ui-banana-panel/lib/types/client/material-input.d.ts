/** Browser file transfer vocabulary shared by picker, clipboard and drag/drop. */
export interface FileTransfer {
    files?: ArrayLike<File> | null;
    items?: ArrayLike<{
        kind: string;
        getAsFile(): File | null;
    }> | null;
    types?: readonly string[];
}
/** Prefer FileList; items is the fallback used by browsers that omit clipboard.files. */
export declare function filesFromTransfer(transfer: FileTransfer | null): File[];
/** Capture file transfers before the original image-only editor; ordinary text is untouched. */
export declare function bindMaterialInputs(card: HTMLElement, receive: (files: File[]) => void, dragging?: (active: boolean) => void): () => void;
//# sourceMappingURL=material-input.d.ts.map