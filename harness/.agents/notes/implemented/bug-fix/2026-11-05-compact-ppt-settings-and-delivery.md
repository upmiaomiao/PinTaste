# Agent Note: Compact PPT settings and deferred delivery

Status: implemented

English | [中文](2026-11-05-compact-ppt-settings-and-delivery.zh.md)

## Problem

The expanded composer card mixed future page/language preferences with an output decision. Stored editable defaults could start native conversion immediately after image review. The delivery panel lacked explicit format choices, and Chinese-only provider instructions conflicted with English content settings.

## Decision

A compact button opens a native popover for slide count and content language. Apply persists the existing conversation record without starting work. Escape, outside dismissal and focus restoration use browser behavior; the narrow viewport uses a bottom sheet. Invalid counts and failed saves remain visible. Existing stored output fields remain compatible but do not appear in the composer or its injected request summary. Explicit output prose remains an ordinary chat request, not a new saved default.

An automatic continuation reaching initial tracing delivers the reviewed images and local image PPTX first, even with an older editable preference. An already-started native batch keeps its recovery path. Explicit editable export still queues native conversion. The delivery panel offers image PPTX, editable PPTX, PDF and image ZIP through the existing user-reviewed chat draft path. A choice does not call tools or authorize paid work. Editable requests ask for an additional call limit and cost estimate before conversion.

Provider writing policy follows the requested content language. Planning uses an English length rule for English copy, and image instructions preserve the language of confirmed visible text. The actual iframe is changed with a hash-locked transformer. The [private-template decision](2026-11-05-private-upload-template-library.md) remains current.

## Alternatives considered

**Hiding the output selector alone.** Would leave a hidden editable default and automatic conversion in place.

**Changing every existing stored preference.** Unnecessary data migration; compatibility reads and changed request behavior suffice.

**Directly generating from format buttons.** Would bypass the existing conversation review and cost boundary.

## Consequences

No schema migration, existing project rewrite, external model call or cloud write is needed. Future automatic continuation changes delivery preference only when it reaches the reviewed-image boundary. Existing files remain available. Export choice still requires confirming the resulting chat request. A real paid English-image/native-deck acceptance test is outside this zero-call validation.

Checks cover preference compatibility and limits, initial versus started native delivery, explicit export queueing, captured English image requests, actual assembled work UI in both languages, and actual SDK format-selection messages. Full framework checks retain the previously documented dependency limitations.
