import type { PropsLocale, PropsRuntime, PropsStore } from '@deepseek-ai/dsh-client-ui-slots';
import type { createBananaPanelStore } from './store.ts';
type TemplateSelectProps = PropsRuntime<'conversation.input.left'> & PropsStore<ReturnType<typeof createBananaPanelStore>> & PropsLocale<'bananaPanel'>;
/** Composer slide template-image dropdown with preset + user templates and upload. */
export declare function TemplateSelect({ useInput, inputActions, useStore, actions, t, }: TemplateSelectProps): import("react").JSX.Element;
export {};
//# sourceMappingURL=TemplateSelect.d.ts.map