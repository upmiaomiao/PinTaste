"""Preserve JSON string whitespace; recover only source-proved literal wraps.

Only raw CR/LF positions carry wrap provenance. Escaped newlines and all spaces
and tabs remain data. Catalog/source checks still belong to the caller.
"""
import json
import re

from .wuyu_errors import PipelineError


class LiteralString(str):
    def __new__(cls, value, literal_breaks=()):
        result = super().__new__(cls, value)
        result.literal_breaks = tuple(literal_breaks)
        return result


class _Pairs(list):
    pass


def _marked_string(token, value):
    breaks, position, i = [], 0, 1
    while i < len(token) - 1:
        ch = token[i]
        if ch == '\\':
            if token[i + 1] == 'u':
                code = int(token[i + 2:i + 6], 16)
                if (0xD800 <= code <= 0xDBFF and token[i + 6:i + 8] == '\\u'
                        and 0xDC00 <= int(token[i + 8:i + 12], 16) <= 0xDFFF):
                    i += 12
                else:
                    i += 6
            else:
                i += 2
        else:
            if ord(ch) < 32 and ch not in '\r\n\t':
                raise ValueError('unknown raw control')
            if ch in '\r\n':
                breaks.append(position)
            i += 1
        position += 1
    if position != len(value):
        raise ValueError('string position mismatch')
    return LiteralString(value, breaks) if breaks else value


def _load(text):
    tree = json.loads(text, strict=False, object_pairs_hook=_Pairs)
    decoder, tokens, i = json.JSONDecoder(strict=False), [], 0
    while i < len(text):
        if text[i] != '"':
            i += 1
            continue
        value, end = decoder.raw_decode(text, i)
        if not isinstance(value, str):
            raise ValueError('string token required')
        tokens.append(_marked_string(text[i:end], value))
        i = end
    iterator = iter(tokens)

    def bind(value):
        if isinstance(value, _Pairs):
            result = {}
            for key, child in value:
                marked = next(iterator)
                if marked != key or marked in result:
                    raise ValueError('duplicate or unmatched key')
                result[marked] = bind(child)
            return result
        if isinstance(value, list):
            return [bind(child) for child in value]
        if isinstance(value, str):
            marked = next(iterator)
            if marked != value:
                raise ValueError('unmatched string')
            return marked
        return value

    result = bind(tree)
    if next(iterator, None) is not None:
        raise ValueError('unmatched tokens')
    return result


def _balanced(text):
    start = text.find('{')
    if start < 0:
        return None
    depth, quoted, escaped = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if quoted:
            if escaped:
                escaped = False
            elif ch == '\\':
                escaped = True
            elif ch == '"':
                quoted = False
        elif ch == '"':
            quoted = True
        elif ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


# op181 A2: a model sometimes drops the closing quote of a short identifier value,
# e.g. "segment_id":"s120,"quote":"..." . Only applied after strict parsing failed; the
# value must look like an identifier, and every downstream source/quote check still runs.
_UNTERMINATED_ID = re.compile(r'("[A-Za-z_][A-Za-z0-9_]{0,40}"\s*:\s*"[A-Za-z0-9_.:\-]{1,80}),(\s*"[A-Za-z_][A-Za-z0-9_]{0,40}"\s*:)')


def _unterminated_identifiers(text):
    return _UNTERMINATED_ID.sub(r'\1",\2', text)


def _trailing_commas(text):
    out, quoted, escaped = [], False, False
    for i, ch in enumerate(text):
        if quoted:
            if escaped:
                escaped = False
            elif ch == '\\':
                escaped = True
            elif ch == '"':
                quoted = False
        elif ch == '"':
            quoted = True
        elif ch == ',':
            j = i + 1
            while j < len(text) and text[j] in ' \r\n\t':
                j += 1
            if j < len(text) and text[j] in '}]':
                continue
        out.append(ch)
    return ''.join(out)


def parse_object(raw):
    if not isinstance(raw, str):
        raise PipelineError('invalid_model_json', '模型没有返回JSON文本。')
    text = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw.strip(), flags=re.I)
    balanced = _balanced(text)
    for candidate in dict.fromkeys([text] + ([balanced] if balanced is not None else [])):
        for variant in dict.fromkeys([candidate, _trailing_commas(candidate), _unterminated_identifiers(candidate),
                                      _trailing_commas(_unterminated_identifiers(candidate))]):
            try:
                result = _load(variant)
            except (ValueError, TypeError, StopIteration, RecursionError, IndexError):
                continue
            if isinstance(result, list) and len(result) == 1 and isinstance(result[0], dict):
                # op 176: a lone object wrapped in an array is the same answer.
                return result[0]
            if not isinstance(result, dict):
                raise PipelineError('invalid_model_json', '模型JSON顶层必须为对象。')
            return result
    raise PipelineError('invalid_model_json', '模型JSON解析失败，未生成伪造的大纲或描摹结果。')


def _candidates(text):
    if not isinstance(text, LiteralString) or not text.literal_breaks:
        return ()
    breaks = set(text.literal_breaks)
    compact = ''.join(ch for i, ch in enumerate(text) if i not in breaks)
    spaced = ''.join((' ' if i == 0 or i - 1 not in breaks else '') if i in breaks else ch
                     for i, ch in enumerate(text))
    return tuple(dict.fromkeys((compact, spaced)))


def identifier(text, allowed):
    if not isinstance(text, str) or text in allowed:
        return text
    matches = [value for value in _candidates(text) if value in allowed]
    return matches[0] if len(matches) == 1 else text


def normalized_fields(mapping, allowed, code='invalid_model_json'):
    if not isinstance(mapping, dict):
        return mapping
    result = {}
    for key, value in mapping.items():
        normalized = identifier(key, allowed)
        if normalized != key and any(other is not key and isinstance(other, str)
                and ''.join(other.split()) == normalized for other in mapping):
            raise PipelineError(code, '模型字段恢复与其他空白键冲突，未采用有歧义的结果。')
        if normalized in result:
            raise PipelineError(code, '模型字段恢复产生重复键，未采用有歧义的结果。')
        result[normalized] = value
    return result


def canonical_schema_fields(mapping, allowed, code='invalid_model_json'):
    """Opt in to CR/LF aliases for a fixed schema's keys, including persisted keys.

    Callers must provide a closed structural field set. This does not assert raw
    JSON provenance, repair spaces/tabs/unknown keys, or alter any field value.
    The default normalized_fields contract remains literal-provenance-only.
    """
    if not isinstance(mapping, dict):
        return mapping
    result = {}
    for key, value in mapping.items():
        normalized = key
        if isinstance(key, str) and key not in allowed:
            candidate = key.replace('\r', '').replace('\n', '')
            if candidate in allowed:
                normalized = candidate
        if normalized != key and any(other is not key and isinstance(other, str)
                and ''.join(other.split()) == normalized for other in mapping):
            raise PipelineError(code, '模型字段恢复与其他空白键冲突，未采用有歧义的结果。')
        if normalized in result:
            raise PipelineError(code, '模型字段恢复产生重复键，未采用有歧义的结果。')
        result[normalized] = value
    return result


def source_text(text, texts):
    if not isinstance(text, str):
        return text
    sources = [value for value in texts if isinstance(value, str)]
    if any(text in value for value in sources):
        return text
    matches = []
    for candidate in _candidates(text):
        if not candidate:
            continue
        count = 0
        for source in sources:
            start = source.find(candidate)
            if start >= 0:
                count += 1 + int(source.find(candidate, start + 1) >= 0)
            if count > 1:
                break
        if count == 1:
            matches.append(candidate)
    return matches[0] if len(matches) == 1 else text


def salvage_reader_object(raw):
    """op181 A1: keep the valid reader findings when ONE item breaks the JSON.

    Only the reader schema is salvaged. Root acknowledgement/issue arrays must parse on
    their own; each finding object is parsed independently and broken ones are dropped
    (counted). Returns (value, dropped) or raises invalid_model_json.
    """
    if not isinstance(raw, str):
        raise PipelineError('invalid_model_json', '模型没有返回JSON文本。')
    text = _unterminated_identifiers(re.sub(r'^```(?:json)?\s*|\s*```$', '', raw.strip(), flags=re.I))
    value = {}
    for key in ('read_material_ids', 'read_segment_ids', 'read_image_ids'):
        match = re.search(r'"' + key + r'"\s*:\s*(\[[^\]]*\])', text)
        if match:
            try:
                value[key] = json.loads(match.group(1))
            except ValueError:
                raise PipelineError('invalid_model_json', '阅读确认字段无法解析。') from None
    start = re.search(r'"findings"\s*:\s*\[', text)
    if not start or not any(key in value for key in ('read_material_ids', 'read_segment_ids')):
        raise PipelineError('invalid_model_json', '模型JSON解析失败，未生成伪造的大纲或描摹结果。')
    tail = text[start.end():]
    end = re.search(r'\]\s*,\s*"(?:issues|read_image_ids|image_findings|read_material_ids|read_segment_ids)"\s*:|\]\s*\}\s*$', tail)
    body = tail[:end.start()] if end else tail
    findings, dropped = [], 0
    for chunk in re.split(r'(?<=\})\s*,\s*(?=\{\s*"segment_id")', body.strip()):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            item = json.loads(chunk)
        except ValueError:
            item = None
        if isinstance(item, dict):
            findings.append(item)
        else:
            dropped += 1
    value['findings'] = findings
    issues = re.search(r'"issues"\s*:\s*(\[.*?\])\s*[,}]\s*(?:"|$)', tail[end.start():] if end else '', re.S)
    if issues:
        try:
            parsed = json.loads(issues.group(1))
            if isinstance(parsed, list):
                value['issues'] = parsed
        except ValueError:
            pass
    if not findings and dropped:
        raise PipelineError('invalid_model_json', '模型JSON解析失败，未生成伪造的大纲或描摹结果。')
    return value, dropped
