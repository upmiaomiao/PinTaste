# Agent Note: Private uploads in the template library

Status: implemented

English | [中文](2026-11-05-private-upload-template-library.zh.md)

## Problem

An uploaded template was available inside its project but absent from All templates. The catalog and saved native designs were separate from project uploads.

## Decision

Project uploads now appear in the owner's library and recommendations. All sources, My templates and Public templates filter before pagination. Only completed templates with existing original files and previews are projected. Metadata is selected in SQLite without reading source text or chunks into the library. Private previews require the owning account, including when the requester is another administrator.

Confirming an upload from its original project uses that material. Confirming it in another owned project creates a disclosed, independent copy of its original and representative previews. The workflow transaction binds the copied material and follows its visual style. Source text, evidence and figures are not copied into the content plan. Existing confirmation, revision and capacity checks remain required. A rolled-back HTTP request removes its newly created files.

The actual iframe is updated using a hash-locked transformer, preserving published language and material-management behavior. The existing [work-state and progress decision](2026-11-05-ppt-work-state-and-template-progress.md) remains current.

## Alternatives considered

**Publishing uploaded templates.** Would disclose private materials to other accounts.

**Automatically copying every upload into another store.** Would retain hidden originals after the user deletes a project material.

**Referencing another project's file directly.** Would break a reused template when its source is deleted. Explicit independent copies make this lifetime visible.

## Consequences

No schema change, model call or cloud write is needed. The library reflects existing project records. Deleting the last explicitly retained copy removes the uploaded template from the library; deleting one project does not erase independently reused copies elsewhere. Generated PPT files follow the existing deletion semantics. Abrupt process termination during a copy can still leave an unreferenced file; normal transaction failures are cleaned up after the request.

Isolated backend checks cover owner isolation, path and hash checks, choice binding, scoped pagination, copy independence, capacity rejection and rollback cleanup. Interface checks use actual SDK iframe messages in both languages and a narrow viewport.
