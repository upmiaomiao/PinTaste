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
export function filesFromTransfer(transfer: FileTransfer | null): File[] {
    if (!transfer)
        return [];
    const files = Array.from(transfer.files || []);
    return files.length ? files : Array.from(transfer.items || []).filter(item => item.kind === 'file').map(item => item.getAsFile()).filter((file): file is File => file !== null);
}
/** op180: a long pasted text becomes a .txt material (like ChatGPT); short text stays in the editor. */
export const PASTED_TEXT_MIN_CHARS = 2000, PASTED_TEXT_MIN_LINES = 40;
export function isLongPastedText(text: string): boolean {
    return !!text && text.trim().length > 0 && (text.length >= PASTED_TEXT_MIN_CHARS || text.split(/\r\n|\r|\n/).length >= PASTED_TEXT_MIN_LINES);
}
export function pastedTextFile(text: string, now: Date = new Date()): File {
    const p = (n: number) => String(n).padStart(2, '0');
    return new File([text], '粘贴文本-' + now.getFullYear() + p(now.getMonth() + 1) + p(now.getDate()) + '-' + p(now.getHours()) + p(now.getMinutes()) + p(now.getSeconds()) + '.txt', { type: 'text/plain', lastModified: now.getTime() });
}
let activeMaterialDrop: object | null = null;
/** Capture file transfers before the original image-only editor; ordinary text is untouched.
 *  op178: in a work session the whole window accepts material files (window capture runs before the
 *  conversation plugin's image-only document overlay). `zone` is only the highlighted region; iframes
 *  (PPT details panel) are made transparent to pointer events during a file drag. Paste stays on the card. */
export function bindMaterialInputs(card: HTMLElement, receive: (files: File[]) => void, dragging: (active: boolean) => void = () => { }, zone: HTMLElement = card) {
    const hasFiles = (transfer: DataTransfer | null) => !!transfer && (Array.from(transfer.types).includes('Files') || Array.from(transfer.items).some(item => item.kind === 'file'));
    const view = zone.ownerDocument?.defaultView || window;
    const token = {};
    activeMaterialDrop = token;
    let shielded: Array<[HTMLIFrameElement, string]> = [], highlighted: [string, string] | null = null;
    const setDragging = dragging;
    dragging = (active: boolean) => {
        if (zone !== card) {
            if (active && !highlighted) { highlighted = [zone.style.outline, zone.style.outlineOffset]; zone.style.outline = '2px dashed rgba(31, 122, 90, 0.75)'; zone.style.outlineOffset = '-6px'; }
            else if (!active && highlighted) { zone.style.outline = highlighted[0]; zone.style.outlineOffset = highlighted[1]; highlighted = null; }
        }
        setDragging(active);
    };
    const mine = (event: DragEvent) => activeMaterialDrop === token && hasFiles(event.dataTransfer);
    const unshield = () => { for (const [frame, value] of shielded) frame.style.pointerEvents = value; shielded = []; };
    const enter = (event: DragEvent) => {
        if (!mine(event)) return;
        event.preventDefault(); event.stopImmediatePropagation();
        if (!shielded.length) {
            shielded = Array.from(zone.ownerDocument.querySelectorAll('iframe')).map(frame => [frame, frame.style.pointerEvents] as [HTMLIFrameElement, string]);
            for (const [frame] of shielded) frame.style.pointerEvents = 'none';
        }
        dragging(true);
    };
    const finish = () => { unshield(); dragging(false); };
    const paste = (event: ClipboardEvent) => {
        const files = filesFromTransfer(event.clipboardData);
        // op180: Office apps add a picture of the selection beside its text; a long text paste means the text.
        const text = event.clipboardData?.getData('text/plain') || '';
        if (isLongPastedText(text) && files.every(file => file.type.startsWith('image/'))) { event.preventDefault(); event.stopImmediatePropagation(); receive([pastedTextFile(text)]); return; }
        if (!files.length) return; event.preventDefault(); event.stopImmediatePropagation(); receive(files);
    };
    const over = (event: DragEvent) => { if (!mine(event)) return; event.preventDefault(); event.stopImmediatePropagation(); if (event.dataTransfer) event.dataTransfer.dropEffect = 'copy'; dragging(true); };
    const leave = (event: DragEvent) => {
        if (!mine(event)) return;
        event.stopImmediatePropagation();
        if (event.relatedTarget === null && (event.clientX <= 0 || event.clientY <= 0 || event.clientX >= view.innerWidth || event.clientY >= view.innerHeight)) finish();
    };
    const drop = (event: DragEvent) => { if (!mine(event)) return; finish(); const files = filesFromTransfer(event.dataTransfer); event.preventDefault(); event.stopImmediatePropagation(); if (files.length) receive(files); };
    // The details panel iframe is same-origin (srcdoc): listen inside it too, so a drag that enters
    // the window over the panel is still accepted (the first event never reaches this window).
    // Keyed by document: loading the srcdoc replaces the initial about:blank document (and its listeners).
    const hookedWindows = new Set<Document>(), seenFrames = new WeakSet<HTMLIFrameElement>();
    const hookFrame = (frame: HTMLIFrameElement) => {
        let inner: Document | null = null;
        try { inner = frame.contentDocument; if (!inner) return; } catch { return; }
        if (hookedWindows.has(inner)) return;
        hookedWindows.add(inner);
        inner.addEventListener('dragenter', enter, true); inner.addEventListener('dragover', over, true); inner.addEventListener('drop', drop, true);
    };
    const scanFrames = () => {
        for (const frame of Array.from(zone.ownerDocument.querySelectorAll('iframe'))) {
            hookFrame(frame);
            if (!seenFrames.has(frame)) { seenFrames.add(frame); frame.addEventListener('load', () => hookFrame(frame)); }
        }
    };
    const frameObserver = new MutationObserver(scanFrames);
    frameObserver.observe(zone.ownerDocument.body, { childList: true, subtree: true });
    scanFrames();
    card.addEventListener('paste', paste, true);
    view.addEventListener('dragenter', enter, true);
    view.addEventListener('dragover', over, true);
    view.addEventListener('dragleave', leave, true);
    view.addEventListener('drop', drop, true);
    view.addEventListener('dragend', finish, true);
    return () => {
        card.removeEventListener('paste', paste, true);
        view.removeEventListener('dragenter', enter, true); view.removeEventListener('dragover', over, true);
        view.removeEventListener('dragleave', leave, true); view.removeEventListener('drop', drop, true); view.removeEventListener('dragend', finish, true);
        frameObserver.disconnect();
        for (const inner of hookedWindows) { try { inner.removeEventListener('dragenter', enter, true); inner.removeEventListener('dragover', over, true); inner.removeEventListener('drop', drop, true); } catch { /* frame gone */ } }
        hookedWindows.clear();
        if (activeMaterialDrop === token) activeMaterialDrop = null;
        finish();
    };
}
