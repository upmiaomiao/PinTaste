import type { PropsLocale, PropsRuntime, PropsStore } from '@deepseek-ai/dsh-client-ui-slots';
import type { createBananaPanelStore } from './store.ts';
type StyleSelectProps = PropsRuntime<'conversation.input.left'> & PropsStore<ReturnType<typeof createBananaPanelStore>> & PropsLocale<'bananaPanel'>;
/** Composer slide visual-style dropdown. */
export declare function StyleSelect({ useInput, inputActions, useStore, actions, t, }: StyleSelectProps): import("react").JSX.Element;
export {};
//# sourceMappingURL=StyleSelect.d.ts.map