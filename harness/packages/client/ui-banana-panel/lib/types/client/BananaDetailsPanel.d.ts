import type { PropsLocale, PropsStore } from '@deepseek-ai/dsh-client-ui-slots';
import type { createBananaPanelStore } from './store.ts';
type BananaDetailsPanelProps = PropsStore<ReturnType<typeof createBananaPanelStore>> & PropsLocale<'bananaPanel'> & {
    closeDetails: () => void;
    writeToConversation: (text: string) => Promise<void>;
};
/** Right-column deck panel; renders an empty hint until a project is set. */
export declare function BananaDetailsPanel({ useStore, t, closeDetails, writeToConversation }: BananaDetailsPanelProps): import("react").JSX.Element;
export {};
//# sourceMappingURL=BananaDetailsPanel.d.ts.map