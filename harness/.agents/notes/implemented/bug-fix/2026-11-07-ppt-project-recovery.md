# Agent Note: Recover the existing PPT project without redundant reading

Status: implemented

English | [中文](2026-11-07-ppt-project-recovery.zh.md)

## Problem

A one-page change can retain a deck type that the execution layer rejects. A cached invalid content plan can repeatedly fail under the same replan action, while feedback that changes the plan also changes every reader cache identity. Generic conflict messages obscure the review or correction needed to continue in the original project.

## Decision

The requirements owner resolves deck plus one page to pages plus one page within its existing transaction. Provenance and field revision follow the accepted update, and idempotency remains based on the original request. Caller-owned patch objects stay unchanged. Invalid combinations do not write requirements or receipts.

Planning feedback belongs to synthesis rather than source reading. The workflow retains the exact reader brief only when its source identity, roles, hashes, exclusions and reading targets remain compatible. Existing historical reader inputs remain usable; changed reading goals or source scope do not inherit unrelated reader context. Actual source and cache integrity checks remain authoritative. Feedback remains available to later planning without granting new source access.

Known deterministic content-plan validation failures from an existing raw cache use the explicit invalid-cache recovery category and retain their safe cause code. The raw response is retained for diagnosis. The same input does not dispatch a replacement paid request automatically; the conversation asks for specific feedback before revising the current draft. Source, path and cache-wrapper integrity failures do not become ordinary plan-correction failures.

The MCP recovery contract maps a closed set of business codes to fixed explanations and next actions. Draft, template and research review, concurrency and quota failures remain distinct. Unknown errors preserve failure without reflecting backend prose or inventing a revision conflict. Readback and error presentation do not approve a draft, change a template or retry paid work. The image-first editable-export route retains its separate explicit request and review contract.

## Alternatives considered

**Persist the requested combination and let projection reject it.** The owner would acknowledge a state that cannot execute, making an ordinary one-page request look like a service failure.

**Repeat synthesis automatically after cached validation fails.** This can repeat paid work without a new user decision and provides no changed input to fix the invalid plan.

**Put all feedback into reader input.** A presentation-structure correction would discard valid extraction results and repeat reading across unchanged materials.

**Reuse readers without checking their scope.** Changed sources or reading goals could silently use unrelated evidence and bypass existing integrity checks.

## Consequences

One-page updates and specific content-plan corrections can continue in the original project while preserving its sources, successful work and historical outputs. Scope and hash changes still invalidate affected work. The owner format and database schema do not change; no historical business data is rewritten in bulk. Model, search, template, image, export, quota and deletion permissions keep their existing checks.

Focused owner/HTTP/Loader, MCP and backend cache tests exercise these contracts with self-created data and mocked external providers. They do not establish real model quality, paid retry cost or high-load fault recovery. Global authorization, duplicate paid-job admission, language policy, process isolation and storage changes are separate work.
