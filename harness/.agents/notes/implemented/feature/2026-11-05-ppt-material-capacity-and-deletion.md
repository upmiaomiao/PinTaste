# Agent Note: PPT material capacity and owner-confirmed deletion

Status: implemented

English | [中文](2026-11-05-ppt-material-capacity-and-deletion.zh.md)

## Problem

Per-file and per-batch upload limits do not bound a project's accumulated extraction data. Oversized material bodies can stall reading preparation before model budgets apply. Owners also need a way to erase uploaded originals without losing generated presentations.

## Decision

The backend limits each project to 50 uploaded files and, by default, 500 MiB uploaded bytes, 2,000,000 extracted characters, 10,000 text chunks, 1,000 source assets, and 24 MiB of serialized parsed materials. Parsing uses remaining project capacity and rejects truncated extraction as incomplete. Reading admission checks the selected evidence before model calls; preparation has a cooperative 60-second deadline. Existing call, point, concurrency, cancellation, and cache rules remain in force.

The details panel uses its existing private iframe-to-Host request channel for `delete_material`. The Host binds the mounted project handle, validates exact request fields, and coordinates the Session upload queue. The cookie-authenticated backend requires project ownership, explicit confirmation, a current revision, and an idempotency ID. The operation is not a model tool or workflow action. Other administrators cannot delete an owner's material through this route.

Deletion refuses active project jobs. An optimistic transaction removes the material and ReferenceFile, invalidates reading and dependent drafts, and records pending cleanup in the existing workflow JSON. New processing and uploads remain blocked until cleanup completes. Validated paths are restricted to the owned project's material, preview, asset, and reading directories; symlinks and generated-output paths are rejected. Physical cleanup is retryable. A bounded completed record retains no source text, filename, or paths.

The selected original and parsed previews are erased along with all shared project reading caches and drafts, which can combine several sources. Other uploaded originals, generated PPT files and exports, and chat history remain. The upload queue releases deleted settled rows and rebinds only remaining unsent materials without uploading or sending a message.

## Alternatives considered

**Exclude material only.** Exclusion is useful for reading scope but retains the original and parsed contents, so it does not satisfy permanent deletion.

**Erase only the selected source cache.** Shared synthesis and reading drafts can quote multiple sources. Clearing all reading caches prevents retaining the deleted material in those derived files.

**Expose deletion as a model tool.** The existing user-only iframe request channel has proven project binding and avoids relying on unverified MCP tool visibility filtering for a destructive operation.

**Allow deletion during processing.** An in-flight parser or reader could recreate deleted content. Admission and the pending-cleanup block prevent that race.

**Increase model context alone.** Model capacity cannot bound local project JSON, extraction memory, or reading preparation.

## Consequences

Confirmation explicitly names permanent erasure, invalidated reading drafts, and retained generated PPT and chat history. Earlier quoted content in chat history is not erased. Deleting one material requires re-confirming the affected draft. There is no database schema migration or cloud-storage change.

Capacity and preparation deadlines bound admission and cooperative work; they do not hard-kill native parser threads. Existing large projects remain readable for management, while oversized selected evidence fails with a corrective message before model use.

Verification uses temporary SQLite databases and real temporary files for deletion and export preservation, actual client business modules for request and queue behavior, and the actual SDK Host and panel in Chinese and English for cancellation, confirmation, active jobs, cleanup retry, capacity failures, and narrow layouts. Full framework GUI/replay commands are blocked by the existing dependency installation setup; baseline and candidate have the same 25 TypeScript errors. These checks do not constitute paid PPT generation or native parser load testing.
