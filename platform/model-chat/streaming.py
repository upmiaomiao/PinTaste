"""Incremental OpenAI SSE and Qwen leading <think> tag parsing."""
import json


async def sse_data(lines):
    data = []
    async for line in lines:
        if line == "":
            if data:
                yield "\n".join(data)
                data = []
        elif line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
    if data:
        yield "\n".join(data)


def event(kind, **values):
    return "data: " + json.dumps({"type": kind, **values}, ensure_ascii=False) + "\n\n"


def same_delta_think_duplicate(native_text, content, *, mode, pending):
    """Recognize only one complete explicit duplicate in this same delta.

    No cross-delta buffering or fuzzy/prefix matching: interrupted streams must
    keep every distinct fragment already received. Ordinary answer text and
    configured implicit think-prefix content are never candidates.
    """
    if (not isinstance(native_text, str) or not native_text or
            not isinstance(content, str) or mode != "start" or pending.strip()):
        return False
    candidate = content.lstrip()
    if not candidate.startswith("<think>"):
        return False
    thought, closing, _ = candidate[7:].partition("</think>")
    return bool(closing) and thought == native_text


class ThinkSplitter:
    """Split explicit tags, or a configured template-prefilled thinking block."""
    def __init__(self, *, think_prefix=False):
        self.mode = "start"
        self.pending = ""
        self.tag_seen = False
        self.think_prefix = think_prefix

    def use_reasoning_field(self):
        # A dedicated reasoning delta makes subsequent content the answer.
        # Do not discard any already-started tagged block.
        if self.mode == "start":
            self.think_prefix = False

    def feed(self, text):
        self.pending += text
        result = []
        if self.mode == "start":
            candidate = self.pending.lstrip()
            if not candidate or ("<think>".startswith(candidate) and candidate != "<think>"):
                return result
            if candidate.startswith("<think>"):
                self.pending = candidate[7:]
                self.mode = "reasoning"
                self.tag_seen = True
            else:
                self.mode = "reasoning" if self.think_prefix else "content"
        if self.mode == "reasoning":
            ending = "</think>"
            if ending in self.pending:
                thought, self.pending = self.pending.split(ending, 1)
                if thought:
                    result.append(("reasoning", thought))
                self.mode = "content"
            else:
                keep = max((n for n in range(1, len(ending))
                            if self.pending.endswith(ending[:n])), default=0)
                ready = self.pending[:-keep] if keep else self.pending
                self.pending = self.pending[-keep:] if keep else ""
                if ready:
                    result.append(("reasoning", ready))
                return result
        if self.mode == "content" and self.pending:
            result.append(("content", self.pending))
            self.pending = ""
        return result

    def finish(self):
        result = []
        if self.pending:
            result.append(("reasoning" if self.mode == "reasoning" else "content", self.pending))
            self.pending = ""
        return result

