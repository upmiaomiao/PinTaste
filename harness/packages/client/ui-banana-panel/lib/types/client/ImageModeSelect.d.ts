import type { PropsLocale, PropsRuntime, PropsStore } from '@deepseek-ai/dsh-client-ui-slots';
import type { createBananaPanelStore } from './store.ts';
type ImageModeSelectProps = PropsRuntime<'conversation.input.left'> & PropsStore<ReturnType<typeof createBananaPanelStore>> & PropsLocale<'bananaPanel'>;
/** Composer slide image-mode dropdown. */
export declare function ImageModeSelect({ useInput, inputActions, useStore, actions, t, }: ImageModeSelectProps): import("react").JSX.Element;
export {};
//# sourceMappingURL=ImageModeSelect.d.ts.map