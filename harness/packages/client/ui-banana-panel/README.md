---
description: "Inspect template designs and generated PPT pages through a conversation."
kind: "package-reference"
---

# @deepseek-ai/dsh-client-ui-banana-panel

English | [中文](README.zh.md)

One Host-owned current-requirements record belongs to each authenticated account and Session. Its values retain field provenance, a revision and the bound project's projection status. Unspecified task, count, content language and output remain automatic. Explicit requirements persist for the current document, including ordinary “this time” wording. Version-four HTTP clients receive the current record; earlier response fields are compatibility projections, and legacy saves preserve fields they cannot represent. The right panel reads this record; it does not maintain another editable configuration. Saving requirements grants no model, search, template, image-review, spending or deletion approval.

## Summary

The work conversation owns requirements and production decisions. The right details column displays recommended designs, private uploads, generated pages and actual exports. It does not expose model configuration to ordinary users.

## Table of Contents

- [Use the panel](#use-the-panel)
- [Ownership and failures](#ownership-and-failures)
- [Model Experience](#model-experience)
- [Dev Note](#dev-note)

## Use the panel

A settled template-selection tool result offers a conversation button. The panel shows task recommendations with at least four neutral preview pages, zoom, library browsing and a separate private-template upload action. A saved personal design is explicitly private and can contain fewer pages. File selection alone never uploads or starts generation. A genuine server confirmation stages a continuation draft, preserving existing text and attachments.

Generated page images appear before editable tracing. Approval drafts contain exact revision and image-version bindings; changed pages require renewed approval. A full native export offers an explicit Save as my template action. Image-only and mixed exports do not qualify.

Material confirmation only uploads, parses and validates the selected files. Prepared cards retain their format glyphs and the unchanged composer draft; only Send or the existing send shortcut submits their context in one ordinary user message. Successful admission consumes exactly the submitted batches; failures retain them, and later additions remain separate. Uploaded descriptors survive a same-tab reload and are revalidated against session ownership and durable submission records before becoming pending again. Local, unconfirmed file bytes cannot survive a page reload. No material confirmation injects, follows up or starts an agent turn ([decision record](../../../.agents/notes/implemented/bug-fix/2026-10-26-material-confirmation-before-send.md)).

Work voice uses the shared platform ASR settings and work permission. It transcribes into the current unsent draft without sending. Unmount, cancellation and late callbacks release recording resources; a transcript remains recoverable if the input becomes busy.

Wuyu Work is an independent application entry. Its settings button embeds the same-origin shared account/settings application without replacing `/work`; closing settings restores work focus. The iframe protocol validates both origin and frame identity and permits only three explicit work destinations. The header shows one circular user icon, name and organization from the safe `/api/me` profile, refreshes after profile saves and opens the embedded account section when clicked. Work history searches the authenticated native work catalog, excluding blank, subagent and other-preset sessions; it never loads Q&A history. Settings can maximize and restore without remounting the iframe or discarding its current content. The work header has no cross-application link ([decision record](../../../.agents/notes/implemented/feature/2026-10-26-work-only-account-settings.md)).

## Ownership and failures

The work header shows a server-authoritative points wallet beside the shared profile. Its popover distinguishes daily, persistent and reserved points, current tariffs and recent ledger entries. Focus, task state changes and explicit refresh update balances without a polling loop. Legacy page balances are not displayed as a second allowance. Administrative grants and policy edits stay in the shared system management application ([decision record](../../../.agents/notes/implemented/feature/2026-10-27-work-points-wallet.md)).

Slot injections own HTTP, input actions and disposal. Components receive typed callbacks and snapshot hooks. Template preview URLs and workflow decisions are checked at wire boundaries. Failed/stale requests cannot approve another project or version. Personal template previews remain account-private. Administrative system-memory controls are outside this browser package and unavailable to ordinary users.

Managed work uses the current origin's `/mcp` and material routes with the account cookie. The browser token constant is empty and empty-token requests omit Authorization; historical build-record service bearers and endpoints are not embedded or used by this application. Private server credentials and the original build record stay unchanged.

Failed upload-limit reads end the loading indicator and preserve the selected local files. Authorization failures retain their specific message. Rechecking only validates the selection; uploading and sending still require their separate user gestures ([decision record](../../../.agents/notes/implemented/bug-fix/2026-10-26-material-upload-preflight-errors.md)).

### Bounded material list and research consent

The composer shows at most three unsent material cards. Successfully sent materials remain in the material list and project detail, while their local File objects and prepared submission payloads are released. The project count includes settled rows; selecting another file cannot bypass the 50-file limit. Each file must be smaller than 50 MiB. Unconfirmed selections can be replaced. The owner can permanently delete an uploaded material from the project details after explicit confirmation; deletion waits for uploads and project jobs to stop. It removes the original and parsed previews, invalidates the current reading drafts and shared caches, and retains generated PPT files and chat history. Pending file cleanup blocks further processing and offers a retry. Settled local rows and unsent attachment notices are reconciled with the remaining server materials; deletion never sends a chat message.

The server also bounds total uploaded bytes and retained extraction data before reading. Capacity failures require reducing the selected evidence or deleting unneeded files; they do not silently truncate a completed document. See the [material capacity and deletion decision](../../../.agents/notes/implemented/feature/2026-11-05-ppt-material-capacity-and-deletion.md) for defaults, ownership checks, and verification limits.

The conversation owns research scope, estimated duration, cost consent, retrieval progress, interruption and follow-up feedback. A replayable first-party Tool observation exposes one current research card outside collapsed process details, including for existing records. Only the newest observed card in a Session remains active. The right details app displays consolidated findings and sources through the research preview target. Authenticated buttons approve research costs; a model Tool call cannot consent for the user. Accepting a report stages an editable continuation draft without sending or generating slides ([decision record](../../../.agents/notes/implemented/feature/2026-10-27-conversation-research-decisions.md)).

Research choices use one confirmation button that explicitly includes the selected round's point use. Existing topic and goal supply the question; edits and new feedback are optional. A failed state read leaves choices visible and confirmation disabled, with distinct recovery messages for login, ownership, missing state, incomplete response and connection failures ([decision record](../../../.agents/notes/implemented/feature/2026-10-27-quick-research-confirmation.md)).

Research controls read the current project revision through a lightweight research endpoint before admission and preserve the same request key and revision when retrying an uncertain submission. Active jobs refresh only the conversation card, including search queries and provider names; the response omits uploaded material bodies and presentation artifacts. Interruption retains sources for a separately confirmed supplement. A user-aborted conversation Turn can pause only its observed research report; disposal or replaying an old interruption cannot pause a later research round. The model sees saved decisions and feedback when it next reads the project workflow, rather than through synthetic Assistant messages.

Research admission is reachable from every validated first-party workflow project observation, including historical MCP text that contains only the server conversation decision. The lightweight authenticated read supplies the actual research state; the model's omission of research fields cannot remove the user's options. Research and template/image review share one composed Turn-tail registration, so chain election cannot hide one behind the other. Current-round searches and reads are visible as a step list with query, provider, result count, representative source titles and explicitly labelled excerpts; prior rounds remain folded. The recorded wire fixture comes from the Agent's actual text serializer and is exercised through the built AppWebEntry composition ([decision record](../../../.agents/notes/implemented/bug-fix/2026-10-27-research-conversation-wire.md)).

Count- or content-language-only changes preserve exact material-reader inputs and evidence context for replanning, while revoking the affected draft approval. Batch parsing retains successful files when another file fails; incomplete material coverage remains visible and requires explicit repair or exclusion. A failed planning response is diagnosed without silently repeating a paid request. Original files and generated outputs remain available in the same conversation. Native-parser process isolation and comprehensive high-load recovery are not provided by this change.

## Model Experience

The model receives actual workflow decisions through existing MCP tool results. Opening a panel does not approve a template, generate images or authorize tracing. Backend gates remain authoritative even when the model asks for an invalid action. Design preferences are server-frozen context, never project evidence or executable skills.

## Dev Note

The focused template/review, voice, wire and task-card tests exercise these boundaries. Build-time public endpoint configuration uses the AI origin; runtime model credentials remain server-side. See the [decision record](../../../.agents/notes/proposed/feature/2026-10-21-wuyu-design-review-memory.md).

Work progress follows an active retry before the previous incomplete draft. Failed transcript rows retain their historical meaning and direct readers to current work details. Template parsing refreshes only while the private-upload view is visible, for at most 120 seconds or 30 reads; completed files without previews are terminal, not still parsing. Library confirmation requires four loaded previews and sufficient source references; private templates require one. A changed draft shows the next step without starting model work ([decision](../../../.agents/notes/implemented/bug-fix/2026-11-05-ppt-work-state-and-template-progress.md)).

The composer offers a read-only Current requirements button beside Web search and Deep research. Changes are described in the conversation. Opening this view and successful local-tool completion refresh the authoritative readback; success and failure rows both offer View current requirements, without copying tool result values into another record. The main attachment button selects materials directly; optional template and Logo purposes remain in its secondary menu. File selection and upload preparation do not submit a conversation message. The delivery panel offers image-based PPTX, editable PPTX, PDF and image ZIP after reviewed page images; native conversion retains its separate explicit request and recovery operations.

All templates includes account-private completed uploads and saved designs with source filters. Cross-project upload reuse creates a disclosed independent copy; public browsing never exposes another account's private uploads ([decision](../../../.agents/notes/implemented/bug-fix/2026-11-05-private-upload-template-library.md)).

## Conversation-first task entry

Users describe their goal or upload materials without choosing Create/Re-layout, slide count or language first. The hero explains this entry; the optional Current requirements view displays explicit choices, inherited values, suggestions and undecided fields. “Create from materials” names the existing-material task. Uploading does not lock the conversation's task identity. A task change keeps original files and historical outputs; a pending or conflicting projection must be resolved before execution.

The model reads get_presentation_requirements and updates validated fields through update_presentation_requirements. An update cites the latest accepted human message, or the verified receipt for its native ask_user_question answer, and an exact nonempty evidence substring returned by the read tool. A receipt pairs the built-in call and successful result, validates every question ID and selected option, and includes only selected labels or typed text. Uploaded text, arbitrary tools and plugin contexts cannot supply that identity. The Host serializes projection to the owned project's brief and denies later planning or generation until its revision matches. An unspecified count cannot silently become twelve slides. Existing confirmations remain authoritative ([decision](../../../.agents/notes/implemented/feature/2026-11-06-conversation-first-requirements.md)).

Failed or paused research with no retained sources offers an in-project “Use uploaded materials” recovery button. A real click reads the latest research and workflow revision, rejects active work or retained reports, and selects materials through the existing user-confirmation operation. Files and the project remain available; recovery does not send a message or generate slides. Research with sources requires a separate preservation and supplement decision. Web and deep search remain selected across submissions in the mounted Session, while each message receives separate authorization; refresh restores the saved choice. See the [recovery decision](../../../.agents/notes/implemented/bug-fix/2026-11-06-ppt-recovery-and-search-mode.md).


Conversation search choices (web, deep, or off) are saved with the PPT preferences. Refresh and another context restore the selected mode. Selection saves no search authorization; each explicit submission still uses the existing intent mechanism. Legacy preference clients preserve this field.


Public template categories distinguish full decks from process flows, roadmaps, graphical abstracts and method figures. Single-image references expose their scope, require their actual preview, and apply only to an asset or one content page. Full-deck confirmation retains five real body references. Both work template browsers expose All and five category buttons; category, source scope and query remain combined across 24-item pages, and switching category returns to the first page ([decision](../../../.agents/notes/implemented/bug-fix/2026-11-06-clickable-template-categories.md)).

The conversation retains output format as delivery intent. Image-first workflow delivery does not replace that preference or invalidate synchronized planning requirements. An owned workflow read can acknowledge an existing pending projection only when every projected requirement and its configuration marker match, with revision and binding checks. Explicit View current requirements retries the authoritative read after a failure; historical tool failures retain their meaning. Reading and acknowledging do not generate slides or approve editable conversion ([decision](../../../.agents/notes/implemented/bug-fix/2026-11-07-ppt-requirements-delivery-recovery.md)).

A one-page requirement resolves to the pages scope in the same owner transaction, including an inherited deck scope. Invalid combinations do not persist a partial requirement or receipt. Recovery preserves already read evidence: planning feedback changes the content plan, while changed source identity or reading goals require a fresh matching reader context. A known invalid cached plan waits for specific user feedback rather than recommending the same paid action again. Safe workflow errors identify the required review, refreshed revision or quota step without exposing backend messages or starting work ([decision](../../../.agents/notes/implemented/bug-fix/2026-11-07-ppt-project-recovery.md)).

The requirements read tool supplies the latest accepted human message ID and a literal excerpt limited to 2,000 UTF-16 units without splitting a surrogate pair. Updates cite an exact substring from that accepted message or verified native question answer. Assistant prose, arbitrary tools and material/plugin results cannot supply human provenance. The Host refuses project creation or research with unrecorded latest question answers or undecided page count. Save the complete agreed scope first; a validated empty update can record unchanged values against the answer receipt. These checks do not grant search, generation or spending approval. Workflow actions still require a valid UUID request_id. Source, revision-conflict and format failures state that the attempted change was not saved. A fixed projection-pending failure states that requirements were saved but not synchronized; an unknown failure asks for authoritative readback without guessing the save outcome ([decision](../../../.agents/notes/implemented/bug-fix/2026-11-08-ppt-requirements-source-feedback.md)).

When requirements change after research, Current requirements can offer an explicit reuse of the existing accepted, non-partial report. The owner reviews suitability and clicks Reuse existing research. A fresh project revision and no active or unknown job are required; the existing UI-only research confirmation boundary is retained. A separate scope receipt preserves the original question, signature, report, selected source material, usage, cache and outputs. Reuse does not fill new scope gaps, search, create a job or generate slides. Deleted/excluded/changed source material and partial reports are not silently restored or accepted.
