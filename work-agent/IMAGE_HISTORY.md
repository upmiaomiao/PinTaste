# Saved slide-image history

Open the original PPT project and select Image history in the details panel. Saved slide images and generated samples remain available after regeneration or a draft reset. Filter by page, preview originals and download individual PNG files. The current-image badge identifies the artifact referenced by the current page; viewing a record does not select or approve it.

Each page has chronological image-record numbers based on retained artifact order. These numbers are not the workflow image_version counter, which can reset after requirements change. Sample records are labeled separately. Old pages remain listed after the current page count changes. The panel renders at most twelve records at once and loads thumbnails lazily. A failed image load retains its history entry and download link.

The existing workflow artifact registry and signed project asset routes provide the files. No new database, storage copy, model call or mutation API is involved. withImageHistory derives UI-only labels before asset URLs become opaque; compact model snapshots omit those labels. Deleted projects or missing source files cannot be recreated by this view.

The deployed panel is maintained through a hash-locked transformer in tools/image-history-ui.cjs and its adjacent JS/CSS sources. Do not rebuild the older TypeScript UI over this resource. Read-only history has no restore, current-version selection or deletion operation.
