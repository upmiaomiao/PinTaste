# PPT reading status waits

The reading completion counter counts whole materials after the reader output is verified. A single-material request can stay at 0/1 while its model call runs. After source reading, the conversation reports page-content planning separately. Neither stage implies that images have been generated.

wait_presentation_workflow performs a read-only wait of at most 25 seconds. wait_status=timeout ends that status wait, while stop_wait=false and current pending_jobs keep the background job active. Follow the returned wait_instruction with the same project handle and job ID; unchanged completion counts alone do not justify manual chunk reading, duplicate analysis or paid retries. A terminal failure, cancellation, pause, review decision or state-read error ends waiting.

The optional conversation.progress.reading fields report verified material, reader-task and text-segment counts and the current reading/planning stage. They do not estimate token-stream percentages. Existing image progress fields and terminal job behavior remain intact.

This deployment checkout contains the published workflow-tools.js rather than its TypeScript authoring tree. tools/reading-wait.cjs reproduces the targeted change from its exact original SHA. It does not rebuild the independently maintained PPT HTML. tests/reading-wait.test.mjs exercises the real tool handlers with a read-only API fixture and no model or external network call.
