import type { PropsLocale } from '@deepseek-ai/dsh-client-ui-slots';
import type { PreviewTarget } from './preview-target.ts';
interface BananaPanelHostProps {
    /** Opaque project handle the panel loads and operates on. */
    projectHandle: string;
    t: PropsLocale<'bananaPanel'>['t'];
    target: PreviewTarget;
    targetRevision: number;
    writeToConversation(text: string): Promise<void>;
}
/** Host the banana panel for one project handle. */
export declare function BananaPanelHost({ projectHandle, target, targetRevision, writeToConversation, t }: BananaPanelHostProps): import("react").JSX.Element;
export {};
//# sourceMappingURL=BananaPanelHost.d.ts.map