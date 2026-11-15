import type { ConversationSlotProps } from '../contract/slots.ts';
/** Suggestions write an editable draft; they never start generation or approve a page. */
export declare function WorkStarters({ t, onChoose, disabled }: {
    t: ConversationSlotProps['t'];
    onChoose: (text: string) => void;
    disabled: boolean;
}): import("react").JSX.Element;
//# sourceMappingURL=WorkStarters.d.ts.map