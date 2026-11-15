# Agent Note: Persist conversation search choices

Status: implemented

English | [中文](2026-11-06-conversation-search-persistence.zh.md)

## Problem

A search choice retained only in a mounted controller disappears after refresh and can disagree with a restored conversation.

## Decision

The existing conversation preference owner stores search_mode as off, web or deep. The composer reads that owner directly and saves clicks without starting research. Submission waits for acknowledged preferences and applies the effective choice to the existing search modifier. Re-layout disables research without erasing the saved new-task preference.

Version-three HTTP responses expose six fields. Version-two responses keep five fields and legacy responses keep three; saves missing search_mode preserve the stored value. Old records normalize to off without migration.

## Alternatives considered

**Local storage only.** Does not restore on a different context or device and separates the preference from its account-owned conversation.

**Reuse search consent.** A persistent choice is not an instruction to repeat research. The existing per-submit intent mechanism stays unchanged.

## Consequences

Web and deep modes remain mutually exclusive. Save failures block submission and surface an error; concurrent writes retain revision checks. No SQL schema change or bulk data rewrite is needed. Rolling back the frontend must retain the compatible six-field Host reader. Keyless checks cover actual HTTP projection, legacy saves, isolated conversations and restored controllers; assembled bilingual pages verify refresh behavior.
