# Agent Note: Classified public figure references

Status: implemented

English | [中文](2026-11-06-public-figure-template-categories.zh.md)

## Problem

Single process or research figures cannot honestly provide five body pages. Importing them as ordinary decks makes visible entries unusable, and unclassified references can be chosen for an unrelated task.

## Decision

An explicit administrator file import validates immutable PNG bytes, hashes, paths and unique identifiers before publishing reviewed references. The approved batch contains sixty-six user-provided figures and twelve existing public system decks. Titles use sequential template numbers. Figure category is a separate filter and recommendation signal, independent of the current research topic.

A trusted single_image catalog scope admits one real loaded preview only for an asset or one content page. Ordinary decks retain five distinct real body references. The UI reads that scope, keeps confirmation separate from generation and retains category during pagination. The MCP schema exposes the same category and scope. Existing private six-axis cards normalize the added optional category to an empty list.

## Alternatives considered

**Duplicate one picture into five pages.** Manufactures reference coverage and hides the source limitation.

**List images without confirmation support.** Leaves newly imported entries visible but unusable.

**Model-classify the import again.** Repeats paid work even though the owner already supplied category folders. Explicit directory classification is recorded instead.

## Consequences

Original local files stay intact; technical PNG previews have a maximum long edge of 4096 pixels. Only reviewed public system references are copied from production; private uploads and production records stay untouched. Template content is visual reference, never factual project evidence. Import has no network or model entry point and changes no SQL schema. Source-free tests cover the complete approved batch, safe failure paths, task scope and category ranking. Component and SDK checks cover one-image confirmation and category forwarding; real paid generation is separate.
