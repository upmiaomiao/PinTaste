# Agent Note: Work state and template parsing progress

Status: implemented

English | [中文](2026-11-05-ppt-work-state-and-template-progress.zh.md)

## Problem

An accepted retry can coexist with its previous incomplete draft. The transcript projection previously called that active retry failed. Private-template parsing also stayed on a stale upload snapshot, and a completed file without previews appeared to be parsing indefinitely. The shipped iframe required five previews while the public choice contract requires four.

## Decision

An explicitly active current job takes priority over an old incomplete draft; a genuinely failed current job and transport errors remain failures. Research-specific progress keeps its existing labels. Failed transcript rows describe the recorded result and point to current work details rather than rewriting history.

The actual iframe artifact is updated through a hash-locked transformer. The legacy React authoring tree is not rebuilt over its newer shipped behavior. Private-template status uses an explicit completed-without-preview terminal state. The upload tab never projects a stale library preview or confirmation target. While the upload view is visible, read-only refresh runs for at most 120 seconds or 30 requests, stops on terminal status, and rejects old project timers. Hidden and disposed views do not dispatch reads. Slow parsing offers manual refresh rather than an unbounded spinner.

Library confirmation requires four loaded previews and an affirmative source-reference sufficiency flag. Private templates require one loaded preview. Confirmation still needs the user's click and the backend's ownership, revision, and template validation. A prior draft invalidated by material or requirement changes shows an explanation without submitting a new reading job or reusing erased sources.

## Alternatives considered

**Treat every incomplete draft as the current task outcome.** Draft validity and current job execution are independent; this mislabels queued or running retries as failures.

**Refresh indefinitely or after leaving the view.** That wastes requests and lets stale callbacks affect later projects. Bounded visible-view reads keep ownership and teardown explicit.

**Enable confirmation from preview count alone.** Four images do not prove enough usable source references. The existing backend rule and the card's sufficiency flag remain required.

**Rebuild the legacy iframe authoring source.** It lacks shipped material, research and locale behavior. A hash-locked edit preserves unrelated behavior until the baseline is reconciled.

## Consequences

These changes affect presentation and read-only refresh, not generation consent or billing. Failed historical records remain visible. Slow or failed parsing still needs an explicit user action. Material erasure and invalidated reading caches retain the [existing deletion semantics](../feature/2026-11-05-ppt-material-capacity-and-deletion.md).

Keyless Tool-state replay pins queued/running retries, real failures and research labels. Actual SDK iframe browser scenarios cover both languages, automatic completion, missing previews, four-page library and one-page private confirmation readiness, and narrow layouts. Timer checks cover project switches, visibility, disposal and bounded polling. No model request is required for these checks.
