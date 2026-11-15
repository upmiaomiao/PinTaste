import { BananaApiError } from './api-client.js';
/** Validate cross-field constraints before the creation request mutates a project. */
export function resolveGenerationIntent(input) {
    const count = input.page_count ?? (input.creation_kind === 'deck' ? 12 : 1);
    const minimum = input.creation_kind === 'deck' ? 2 : 1;
    const maximum = input.creation_kind === 'deck' ? 80 : input.creation_kind === 'pages' ? 8 : 1;
    if (!Number.isInteger(count) || count < minimum || count > maximum)
        throw new BananaApiError(`所选制作方式需要 ${minimum}–${maximum} 页，请调整页数。`, 400);
    return { creation_kind: input.creation_kind, page_count: count,
        image_profile: input.image_profile, output_preference: input.output_preference ?? (input.creation_kind === 'deck' ? 'editable' : 'image'),
        asset_kind: input.asset_kind };
}
