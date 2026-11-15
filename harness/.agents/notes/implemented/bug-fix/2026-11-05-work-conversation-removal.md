# Agent Note: Visible work conversation removal

Status: implemented

English | [中文](2026-11-05-work-conversation-removal.zh.md)

## Problem

Work conversations exposed archive only in a hover menu, and blank sessions had no menu. The Router also rejected the native archive RPC. Integrated work history ignored the existing archive set, so removed rows could reappear in another surface.

## Decision

Show a localized trash button on every session row. Both the button and existing Archive action require a confirmation stating the retention boundary. Cancel and Escape do not mutate; errors retain the dialog for retry. Running work, running descendants, pending interactions and active schedules disable removal. Confirmation uses the existing durable workspace archive set and current-selection clearing. It does not erase session logs, PPT files, materials or usage, or cancel submitted work.

Add an optional boolean `excludeArchived` to the existing session/list request. Only browsing callers opt in. After listing authorized summaries, the controller consults the current workspace archive set. The test Router history reader requests this exclusion; running-job readers retain the default full list. The existing archive RPC is narrowly added to the Router allowlist: exactly one validated sessionId in request, through the authenticated tenant Host registry. Other workspace management and optional RPCs remain restricted. No new native RPC or schema migration is introduced.

The global archive storage decision remains unchanged. This note supersedes only its earlier no-confirmation UI decision. Full Typert generation fails in the partial checkout because remote artifact discovery reports no Remote methods. A checked-in hash-locked metadata reproducer adds the optional boolean to the existing Host and Client request codecs; decoding tests exercise both published codecs. Other metadata is byte-preserved.

## Alternatives considered

**Permanent source deletion.** Would erase retained work and require a separate lifecycle decision.

**Client-only list filtering.** Would leave integrated history inconsistent.

**Changing the default session/list response.** Could conceal archived running work from control readers; explicit opt-in preserves compatibility.

## Consequences

Removal persists across refresh through the existing registry. There is no unarchive UI. Current listing and confirmation are not an atomic task-cancellation boundary; submitted work continues. Tests cover blank rows, cancellation, retry, active-state controls, race-safe list filtering, strict wire validation, the Router ownership boundary and bilingual assembled UI. Live removal acceptance uses only a newly created fixture, with no model call or original record removal.
