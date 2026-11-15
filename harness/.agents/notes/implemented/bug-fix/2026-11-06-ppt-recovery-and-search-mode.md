# Agent Note: Recover research failures without replacing the project

Status: implemented

English | [中文](2026-11-06-ppt-recovery-and-search-mode.zh.md)

## Problem

A failed public-query plan can stop PPT preparation before any source is collected. Generic failure guidance gives no usable path back to uploaded materials and can lead the assistant to describe the project as permanently locked. A search switch that clears after sending also makes consecutive requests require the same selection again.

## Decision

Trusted workflow results advertise an in-project recovery choice for failed or paused research with no retained sources. A real user click selects existing materials through the existing research-confirmation operation. The click reads current research and workflow state, requires the same revision and no current pending work, and uses revision-checked submission. Changed questions, active jobs and retained reports or sources prevent this shortcut. Selecting existing materials preserves the project and files; it neither submits a chat message nor starts generation.

The Session search controller keeps the selected web or deep mode after submission. Each later submission still receives its own authorization and marker. Explicitly switching off stops future search authorization; re-layout keeps search disabled. This preference lasts for the mounted Session controller, with refresh returning to off. A selected mode does not itself start a job or authorize paid retries.

## Alternatives considered

**Replace the project after every failure.** Discards the connection to existing uploads, drafts and results without addressing a failed query plan.

**Retry research or waive validation automatically.** Repeats paid work or admits invalid queries without a fresh user decision.

**Keep search authorization reusable across messages.** Confuses a persistent visible preference with permission for a particular submission. Each message retains its own admission.

## Consequences

Users can continue with uploaded materials in the same project after a source-free research failure. Research that has retained sources requires a separate preservation and supplement decision. Material parsing and research-plan failures remain distinct; this change does not weaken source, quota, template or image-review checks, and does not change database structure or migrate business data.

Focused checks cover three backend cases, nine MCP cases, eighteen recovery-client cases and five search-controller cases without real model or cloud calls. Client builds succeed; the baseline and candidate each retain forty-nine lines of existing type diagnostics, so the full type check is not reported as passing. Real paid generation and the outer chat-agent flow remain separate verification work.
