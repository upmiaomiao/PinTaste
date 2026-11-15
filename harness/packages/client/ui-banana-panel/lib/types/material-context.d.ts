/** Authenticated attachment-to-conversation association; browser text is never evidence. */
import type { IncomingMessage, ServerResponse } from 'node:http';
import type { Agent } from '@deepseek-ai/dsh-agent';
interface MaterialContextHost {
    reject(request: IncomingMessage): 401 | 403 | undefined;
    resolveAgent(id: string): Promise<Agent | undefined>;
    readWorkflow(agent: Agent, handle: string, signal: AbortSignal): Promise<unknown>;
    enqueue(agent: Agent, text: string, summary: string): void;
}
/** Fixed public diagnostics only; never carry upstream text or capabilities. */
export declare class MaterialContextFailure extends Error {
    readonly status: number;
    readonly code: string;
    constructor(status: number, code: string);
}
/** Classify only original Harness error codes, without exposing provider messages. */
export declare function workflowToolFailure(code?: string): MaterialContextFailure;
/** Own one route's in-flight deduplication and return its bounded HTTP handler. */
export declare function createMaterialContextHandler(host: MaterialContextHost): (req: IncomingMessage, res: ServerResponse) => Promise<void>;
export {};
//# sourceMappingURL=material-context.d.ts.map