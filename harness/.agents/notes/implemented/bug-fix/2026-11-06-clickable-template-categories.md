# Agent Note: Clickable categories in both template browsers

Status: implemented

English | [中文](2026-11-06-clickable-template-categories.zh.md)

## Problem

The work conversation panel and the project-details app are separate template browsers. A category control in only one browser leaves the other with a long unfiltered list. A host bridge that drops the category also makes a visible control ineffective.

## Decision

Both browsers expose All and five category buttons: full decks, process diagrams, technical roadmaps, graphical abstracts and methods diagrams. Labels follow the interface language; requests retain the server's stable Chinese category values. Category, account-private/public source scope and search combine in the existing authenticated list operation. Changing category resets the offset; pagination retains all filters and uses 24 items. Loading hides the previous category's cards, while empty and failed list states use their own status and retry the current filters. Reading or switching filters does not confirm a template or start model work.

The project-details postMessage bridge forwards the validated category to its existing list executor. A trusted single_image reference admits one actual loaded preview only when reference_sufficient remains true; ordinary decks keep the existing multi-page requirements.

The runtime detail app is generated from its SHA-locked deployed HTML by [template-category-ui.cjs](../../../../../work-agent/tools/template-category-ui.cjs). The generator edits five known functions and adds category helpers without rebuilding the older authoring tree. The React component uses the same category values and preserves its original confirmation operation.

## Alternatives considered

**Keep a dropdown in one browser.** Leaves the other consumer unfiltered and hides the available category choices.

**Filter the loaded page locally.** Omits matching entries on other pages and reports incorrect totals.

**Rebuild the old detail-app authoring tree.** Can discard deployed material, research, history and localization behavior that the tree does not contain.

## Consequences

All retains uncategorized private uploads; a specific category excludes records without that category. Filtering changes viewing state, not the selected project template or generated files. The server retains ownership checks and template-use restrictions. No SQL schema, model configuration, shared-library contents or cloud policy changes are part of this decision.

Focused verification observes category forwarding, combined source/search filters, pagination, stale responses, empty/error recovery and bilingual narrow-screen rendering through the real client and detail-app code. Full-framework type/GUI checks, every model's compatibility and paid generation are separate coverage limits; this note does not claim those checks pass.
