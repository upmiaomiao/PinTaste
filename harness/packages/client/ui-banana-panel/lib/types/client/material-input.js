/** Prefer FileList; items is the fallback used by browsers that omit clipboard.files. */
export function filesFromTransfer(transfer) {
    if (!transfer)
        return [];
    const files = Array.from(transfer.files || []);
    return files.length ? files : Array.from(transfer.items || []).filter(item => item.kind === 'file').map(item => item.getAsFile()).filter((file) => file !== null);
}
/** Capture file transfers before the original image-only editor; ordinary text is untouched. */
export function bindMaterialInputs(card, receive, dragging = () => { }) {
    const hasFiles = (transfer) => !!transfer && (Array.from(transfer.types).includes('Files') || Array.from(transfer.items).some(item => item.kind === 'file'));
    const paste = (event) => {
        const files = filesFromTransfer(event.clipboardData);
        if (!files.length)
            return;
        event.preventDefault();
        event.stopImmediatePropagation();
        receive(files);
    };
    const over = (event) => {
        if (!hasFiles(event.dataTransfer))
            return;
        event.preventDefault();
        event.stopImmediatePropagation();
        if (event.dataTransfer)
            event.dataTransfer.dropEffect = 'copy';
        dragging(true);
    };
    const leave = (event) => {
        if (!(event.relatedTarget instanceof Node) || !card.contains(event.relatedTarget))
            dragging(false);
    };
    const drop = (event) => {
        dragging(false);
        const files = filesFromTransfer(event.dataTransfer);
        if (!files.length)
            return;
        event.preventDefault();
        event.stopImmediatePropagation();
        receive(files);
    };
    card.addEventListener('paste', paste, true);
    card.addEventListener('dragover', over, true);
    card.addEventListener('dragleave', leave, true);
    card.addEventListener('drop', drop, true);
    return () => { card.removeEventListener('paste', paste, true); card.removeEventListener('dragover', over, true); card.removeEventListener('dragleave', leave, true); card.removeEventListener('drop', drop, true); dragging(false); };
}
//# sourceMappingURL=material-input.js.map