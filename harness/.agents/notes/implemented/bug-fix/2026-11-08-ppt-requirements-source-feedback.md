# Agent Note: PPT requirements source and failure feedback

Status: implemented

English | [中文](2026-11-08-ppt-requirements-source-feedback.zh.md)

## Problem

An update can fail its source check before the requirements owner saves anything. Presenting every failed update as an unfinished synchronization incorrectly implies that the attempted change was saved. A model can also paraphrase a human confirmation or quote a tool result while selecting the correct human message ID, causing the unchanged exact-evidence check to reject it.

## Decision

The read tool returns the latest accepted human message ID with a bounded literal excerpt. The excerpt has at most 2,000 UTF-16 units and never ends with half a surrogate pair. Tool instructions require the original wording and a fresh read after a new human reply. Only an accepted user/message event whose source kind is user supplies human provenance. Tool, assistant and plugin messages do not become human sources.

The client maps a single exact fixed tool-error code to localized source, conflict, format or pending-projection feedback. Source, conflict and format failures do not claim the attempted change was saved. Pending projection distinguishes saved requirements from their project synchronization. Unknown or mixed errors ask for current readback without guessing the save outcome or displaying raw errors. Historical failed cards remain failed, and recovery retains the existing explicit read action.

## Alternatives considered

**Treat every failed update as pending synchronization.** Source and revision failures can happen before saving, so this would misstate the attempted change's outcome.

**Accept tool answers as human provenance.** Tool results do not establish an accepted human source. The existing selector and exact-evidence check remain authoritative.

**Display raw error text.** Fixed locale-owned categories explain the next step without exposing arbitrary runtime text.

## Consequences

A keyless standard Loader fixture uses real Agents, Sessions, ToolRuntime errors and React rendering. It verifies literal evidence, stale IDs, paraphrases, plugin rejection, a new accepted human reply, bounded excerpts and bilingual fixed-error feedback. The source selector and exact-evidence guard remain byte-identical. A tool result by itself is not an accepted human reply; this change does not modify the ask-user bridge, automatically retry an update, write an original failed user patch, or authorize generation.
