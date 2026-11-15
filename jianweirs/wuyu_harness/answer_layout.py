"""Reader-sized paragraphs without dropping evidence or rewriting conclusions."""
from __future__ import annotations

import re


_NUMBER = re.compile(r'(?<![\w\[(（])(?P<label>\d{1,2})[)）]\s*|(?P<circled>[①②③④⑤⑥⑦⑧⑨⑩])')


def public_review_text(value: str) -> str:
    """Two public stage names: 见微识别 (the detector) and 独立复核 (any other
    reviewer). Provider brands are hidden; raw audit data stays unchanged."""
    text = str(value or '')
    text = re.sub(r'见微初判\s*(?:与|和|及|→|/|、)\s*Gemini(?:\s*[\d.]+)?(?:\s*Flash)?\s*(?:全面|综合|最终)?复核', '见微识别与独立复核', text, flags=re.I)
    modifiers = r'(?:(?:全面|综合|最终|交叉|同图|场景|误报|环境|影像|科学|高分|光学|框选|与|和|及)\s*)*'
    text = re.sub(r'\bGemini(?:\s*[\d.]+)?(?:\s*Flash)?\s*' + modifiers + r'(?:复核|审查|审阅|评审|终审)', '独立复核', text, flags=re.I)
    text = re.sub(r'\bDeepSeek\s*(?:(?:主模型|模型|综合|最终)\s*)*(?:复核|审查|审阅|终审)', '独立复核', text, flags=re.I)
    text = re.sub(r'见微初判|见微初评', '见微识别', text)
    return text.replace('见微复核', '独立复核')


def _paragraphs(block: str, target: int = 180) -> str:
    if len(block) <= target or re.match(r'\s*(?:[-*+]\s|\d+\.\s|#{1,6}\s|\|)', block):
        return block
    # Chinese sentence punctuation is safe around URLs, source IDs and decimals.
    sentences = re.findall(r'[^。！？]+[。！？]?|[。！？]', block)
    if len(sentences) < 2:
        sentences = re.findall(r'[^；]+[；]?|[；]', block)
    paragraphs, current = [], ''
    for sentence in sentences:
        if current and len(current) + len(sentence) > target:
            paragraphs.append(current.strip())
            current = ''
        current += sentence
    if current.strip():
        paragraphs.append(current.strip())
    return '\n\n'.join(paragraphs) or block


def readable_blocks(value: str) -> str:
    """Reflow real prose and explicit inline lists; preserve fenced/quoted data."""
    text = str(value or '').replace('\r\n', '\n').strip()
    pieces = re.split(r'(```[\s\S]*?```)', text)
    result = []
    for piece in pieces:
        if piece.startswith('```'):
            result.append(piece)
            continue
        blocks = []
        for block in re.split(r'\n\s*\n', piece):
            if not block.strip():
                continue
            matches = list(_NUMBER.finditer(block))
            # Lists already formatted by the writer keep their existing layout.
            if len(matches) >= 2 and not re.search(r'^\s*(?:[-*+]\s|\d+\.\s)', block, re.M):
                intro = block[:matches[0].start()].strip()
                items = []
                for i, marker in enumerate(matches):
                    stop = matches[i+1].start() if i+1 < len(matches) else len(block)
                    content = block[marker.end():stop].strip()
                    label = marker.group('label')
                    prefix = f'{label}. ' if label else '- ' + marker.group('circled') + ' '
                    items.append(prefix + content)
                blocks.append((_paragraphs(intro) + '\n\n' if intro else '') + '\n\n'.join(items))
            else:
                blocks.append(_paragraphs(block))
        result.append('\n\n'.join(blocks))
    return '\n\n'.join(part for part in result if part)


def concise_lead(value: str, target: int = 220) -> tuple[str, str]:
    """Select complete opening sentences; keep all remaining text as detail."""
    text = str(value or '').strip()
    if len(text) <= target:
        return text, ''
    matches = list(re.finditer(r'[^。！？\n]+[。！？]?|[。！？]', text))
    if len(matches) < 2:
        # A single long sentence remains intact rather than being cut mid-claim.
        # It will be collapsed by the client while the full wording is available.
        return text, ''
    end = matches[0].end()
    index = 1
    while index < len(matches) and matches[index].end() <= target:
        end = matches[index].end()
        index += 1
    return text[:end].strip(), text[end:].strip()


def normalize_answer(summary: str, sections: list[dict]) -> dict:
    lead, remainder = concise_lead(public_review_text(summary))
    output = []
    if remainder:
        output.append({'heading': '判断依据与补充', 'content': readable_blocks(remainder)})
    for section in sections:
        if not isinstance(section, dict) or not str(section.get('content') or '').strip():
            continue
        output.append({'heading': public_review_text(str(section.get('heading') or '补充说明')),
                       'content': readable_blocks(public_review_text(str(section['content'])))})
    return {'summary': readable_blocks(lead), 'sections': output, 'layout_version': 'answer-layout-v14'}
