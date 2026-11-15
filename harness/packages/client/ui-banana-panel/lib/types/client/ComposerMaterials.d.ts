import type { InjectFace, PropsLocale, PropsRuntime, PropsStore } from '@deepseek-ai/dsh-client-ui-slots';
import type { ObservableSnapshot } from '@deepseek-ai/dsh-client-store';
import type { createBananaPanelStore } from './store.ts';
import { type MaterialRole } from './material-upload.ts';
import type { UploadSnapshot } from './material-queue.ts';
export interface MaterialUploadsInjected {
    hooks: {
        materialUploads: ObservableSnapshot<UploadSnapshot>;
    };
    intakeFiles(files: File[], role: MaterialRole, handle: string | null): Promise<void>;
    retryUpload(id?: string): void;
    pauseUpload(id: string): void;
    removeUpload(id: string): void;
    syncUploads(): Promise<void>;
    openDetails(): void;
}
type Props = PropsRuntime<'conversation.input.left'> & PropsStore<ReturnType<typeof createBananaPanelStore>> & PropsLocale<'bananaPanel'> & InjectFace<MaterialUploadsInjected>;
/** Material files remain in the Session upload controller; the view handles gestures only. */
export declare function ComposerMaterials({ sessionId, useSessions, useStore, useMaterialUploads, actions, t, openDetails, intakeFiles, retryUpload, pauseUpload, removeUpload, syncUploads }: Props): import("react").JSX.Element;
export {};
//# sourceMappingURL=ComposerMaterials.d.ts.map