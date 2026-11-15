# Agent Note: Recover PPT requirements after image-first delivery

Status: implemented

English | [中文](2026-11-07-ppt-requirements-delivery-recovery.zh.md)

## Problem

The workflow delivers approved page images before an optional editable conversion. Its runtime output format can therefore differ from the conversation's saved delivery preference. Treating that runtime field as a planning requirement creates a false pending projection. Reusing one projection request ID for different observed states can also collide with a previous receipt. A failed current-requirements card needs an explicit authoritative read to recover its view.

## Decision

The conversation remains the owner of output preference, while brief projection compares planning fields and the configuration revision marker. Output preference does not force the runtime brief's delivery state. Direct model writes to managed brief fields remain restricted; editable export retains its separate confirmation, image review and execution checks.

Projection request identity includes the conversation revision, project identity, observed workflow revision and a stable patch fingerprint. Repeating the same observed operation keeps its identity; a later state or different patch uses a different identity.

A successful owned-project read can acknowledge an existing pending projection only when the projected content and configuration marker match. The read rejects a different binding, an invalid or older observed workflow revision, and a concurrent owner revision change. This acknowledgement changes synchronization metadata, without changing saved preferences or project content. Genuine template-confirmation changes retain their existing synchronization path. Failed reads keep saved requirements available without claiming a successful acknowledgement.

The tool row distinguishes a failed read from a failed update through its registered operation. View current requirements explicitly reloads the authoritative controller, including from an error or conflict. Historical failed results remain failed; tool result text is not another configuration owner.

## Alternatives considered

**Force editable preference into every workflow brief.** Image-first delivery would continually undo it, and a preference would be confused with conversion execution.

**Clear pending unconditionally on a read.** A genuine content mismatch, stale configuration marker or failed ownership read would then be reported as applied.

**Use a random projection request ID on every retry.** An uncertain response could repeat the same operation instead of reusing its receipt.

## Consequences

Existing matching projects can recover in their conversation without a new model request or recreating the project. Genuine mismatches remain pending, and a saved editable preference still requires the explicit export path. Focused durable-store, Loader and client-controller tests cover these boundaries. This change does not add automatic retries of failed model work or repair unrelated material, provider or native-parser failures.
