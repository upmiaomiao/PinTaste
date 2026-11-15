import type { PropsLocale } from '@deepseek-ai/dsh-client-ui-slots';
import type { ToolCallViewProps } from '@deepseek-ai/dsh-client-ui-tool/client';
import type { PreviewTarget } from './preview-target.ts';
type BananaPanelProps = ToolCallViewProps & PropsLocale<'bananaPanel'> & {
    /** Write the project handle to the shared store and open the details column. */
    rememberProject: (handle: string, projectId?: string) => void;
    openPanel: (handle: string, projectId?: string, target?: PreviewTarget) => void;
};
/** Inline row that routes one deck project into the right details panel. */
export declare function BananaPanelRow(props: BananaPanelProps): import("react").JSX.Element;
export {};
//# sourceMappingURL=BananaPanelRow.d.ts.map