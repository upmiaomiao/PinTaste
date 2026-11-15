# Agent Note: Conversation-owned PPT requirements

Status: implemented

English | [中文](2026-11-06-conversation-first-requirements.zh.md)

## Problem

A conversation's settings and a project's brief can diverge when they remain independently editable. Upfront selectors also force users to decide task type, slide count and language before describing their goal. Broad invalidation and a single failed-file outcome can discard usable preparation or make the conversation appear blocked.

## Decision

The authenticated Session Host owns one current-requirements record with values, per-field provenance, revision, project binding and projection status. Automatic values remain undecided; ordinary “this time” requirements persist for the current document. Existing HTTP responses are projections of this owner, not another editable record. Legacy writes preserve fields their clients cannot represent. No bulk historical rewrite or SQL schema migration is required.

Local requirements tools read this record and accept validated patches with the latest accepted human message ID and an exact nonempty evidence substring. Source material and plugin messages cannot act as human update evidence. A saved patch is projected serially to the owned project's brief. Planning and generation wait for the applied revision; undecided counts cannot fall through to a twelve-slide default. Template, research, spending, final-image and deletion checks remain separate.

The hero and attachment entry support direct dialogue and material intake. Uploading does not lock the task. The right summary and actual project-details app show the same Host record and provenance; workflow results cannot replace that record. Host-context notifications update the iframe without navigating it or discarding feedback. The [runtime generator](../../../../../work-agent/tools/conversation-requirements-ui.cjs) edits the SHA-locked deployed HTML rather than rebuilding the older authoring tree.

Count- and language-only replanning retains exact material-reader inputs and evidence context, then requires renewed review of the affected draft. Other substantive content changes retain their input-dependent invalidation. A batch parser records individual failures and preserves successful members. Failed cached planning responses expose a recovery diagnostic without automatically repeating paid work. Task changes preserve source files and historical outputs in the conversation; pending conflicts do not grant execution.

## Alternatives considered

**Keep selectors and project fields independently editable.** Multiple owners can report different effective requirements.

**Put job state and approval into the requirements record.** A preference change would then look like permission to execute or retry work.

**Restart the whole project after any failure.** Discards useful preparation and obscures which input or step needs attention.

## Consequences

Requirements express the current goal; jobs and their immutable input versions still describe execution. Updating a requirement does not approve a template, search, image generation, native conversion or deletion. The material, reference, quota, version and ownership checks remain authoritative. A user can continue discussing requirements while a service or projection needs repair.

Focused verification covers requirements storage and compatibility, accepted-message evidence, projection gates, count/language cache reuse, batch partial failure, and both browser consumers. Native-parser hard isolation, comprehensive high-load recovery and a guarantee that every model failure completes automatically remain outside this change. Full-framework type/GUI and paid end-to-end generation require their own evidence.
