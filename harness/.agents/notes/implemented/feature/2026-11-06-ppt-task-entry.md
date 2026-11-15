# Agent Note: PPT task entry and shared presets

Status: implemented

English | [中文](2026-11-06-ppt-task-entry.zh.md)

## Problem

Hero cards that only append suggested text leave the saved page/type choices unchanged. An uploaded original can then be treated as reference material for a default new deck. New-task preferences can also disagree with a historical re-layout project.

## Decision

One Session-owned preference controller serves the task selector, creation cards and settings popover. Cards save a creation type and compatible count without modifying the draft. The two task entries separate creation from original-PPTX re-layout. Attachments and bound projects fix task identity; another conversation owns a different task. A project observation supplies its effective mode without persisting over new-task defaults.

The selector records ordinary logged task text before sending. Upload-based creation also passes task_mode, creation_kind and count to the existing MCP creation operation. Re-layout projects reject generation and reading-model actions until local preparation succeeds. Version-two preference reads expose the extra fields; legacy reads project three fields and legacy saves preserve task choices. Stored legacy records normalize on read, without a bulk rewrite.

Hero suggestions use a session-maybe slot. The Session project-store handle remains mounted only in Session slots; observable preference snapshots are shared with the hero without mounting that handle in a second scope.

## Alternatives considered

**Appending more prompt text.** Leaves the visible settings and saved choice inconsistent.

**Separate state for cards.** Creates another independently editable page/type default.

**Switching an existing project silently.** Risks mixing original-source and reference-material semantics or discarding an accepted page plan.

## Consequences

Input language and output-format ownership remain separate. Mode selection saves no generation approval; re-layout keeps original-page, template and final-image checks. Existing projects and original files remain intact. No SQL schema or historical-data migration is required.

Keyless checks cover the real preference HTTP handler, actual MCP SDK creation calls, original-page Worker regression, assembled desktop UI in two languages, refresh and captured submitted text. Owner-local expected text pins the new model-visible summary. Full framework type checking retains declaration drift for existing slot/API additions; the 430-pixel shell is checked after its native responsive transition completes. No real model request or paid generation is used for these checks.
