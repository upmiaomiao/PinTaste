# Agent Note: Work composer full-deck page count

Status: implemented

## Problem

Work requests need a visible exact page count that is retained with the request and does not start generation when selected.

## Decision

A per-Session composer choice accepts either written requirements or an exact 1–80-slide PPT count. One uses the existing focused-page mode without an additional cover; 2–80 uses full-deck mode including the cover. Submission records the selected count in the ordinary user message. Invalid intermediate number edits block submission before the existing search modifier can arm research. Session and plugin disposal remove the shared submission modifier. Single-image and focused-page requests retain their own scope.

## Alternatives considered

Enabling the entire legacy generation toolbar also exposes model and output controls outside this change. Changing the backend default cannot represent the user's explicit choice. The count control uses the existing submission extension instead.

## Consequences

The choice stays within the current Session and does not change the backend workflow or begin model calls. The existing Agent saves the requirement through the current project API; backend bounds and user review remain authoritative. Verification covers count admission, invalid drafts, Session isolation, retries, component output, candidate bundling and the assembled workbench with model-bound submission blocked and inspected.
