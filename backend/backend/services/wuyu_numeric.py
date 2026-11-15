"""Strict numeric grounding for visible copy and cited original PDF pages."""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re
import unicodedata
from .wuyu_errors import PipelineError, check_cancel

# ASCII suffixes can be real units (24GB, 5mg), so they must not hide a
# numeric claim. The suffix boundary only prevents partial decimal/% matches.
NUMBER = re.compile(r'(?<![A-Za-z0-9_.])(?P<value>[+-]?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?)(?P<percent>\s*%)?(?![0-9_%]|\.\d)')
# Normalize only complete ASCII thousands groups, before NFKC can turn a
# Chinese fullwidth comma into ASCII punctuation. Invalid groups and spaced
# lists stay separate values; no rounding, unit scaling or sign change occurs.
THOUSANDS = re.compile(r'(?<![A-Za-z0-9_.,])[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?(?:[eE][+-]?\d+)?(?![0-9_,]|\.\d)')
STANDARD_ID = re.compile(r'(?<![A-Za-z0-9])(?P<family>GB(?:\s*/\s*T)?|T\s*/\s*[A-Z]{2,15})\s*'
                         r'(?P<serial>\d{1,7}(?:\.\d{1,3})?)\s*[-—–]\s*(?P<year>(?:19|20)\d{2})(?!\d)', re.I)
REFERENCE_UNIT = re.compile(r'(?:例如|如)\s*(?P<quantity>1(?:\.0+)?\s*(?:立方米|m[³3]|千克|kg|吨))(?![A-Za-z0-9])')
GRADE_LABELS = re.compile(r'(?<![\d.])1\s*级\s*[、，,/／]\s*2\s*级\s*(?:[、，,/／]|和|及)\s*3\s*级(?!\d)')
ONE_APPLICATION = re.compile(r'(?<![\d.])1\s*项')
FACTUAL_AUTHORITY = re.compile(r'现行标准|国标规定|国家标准规定|标准要求|已发布|已获批|实测|实际累计|实际统计|已完成')

# A quantity written with a scale word equals the same quantity written another way: "9 млн",
# "9 million" and "900万" are all 9,000,000. A translated page must not be refused because the
# language groups digits differently. Only the VALUE is normalised; units, sign and rounding are
# never touched, and a bare number is never rescaled. 千 before a metric unit (千克, 千瓦) is the
# SI prefix, not a quantity word, and is left alone.
SCALE_WORDS = ((r'千万', 10**7), (r'百万', 10**6), (r'万亿', 10**12), (r'十亿', 10**9), (r'亿', 10**8), (r'万', 10**4),
               (r'千(?!克|米|瓦|焦|帕|卡|伏|安|赫|字节|兆)', 10**3),
               (r'billions?(?!\w)', 10**9), (r'млрд(?!\w)\.?', 10**9), (r'миллиард\w*', 10**9),
               (r'millions?(?!\w)', 10**6), (r'mln(?!\w)\.?', 10**6), (r'млн(?!\w)\.?', 10**6), (r'миллион\w*', 10**6),
               (r'thousands?(?!\w)', 10**3), (r'тыс(?!\w)\.?', 10**3), (r'тысяч\w*', 10**3), (r'мың(?!\w)', 10**3))
SCALE_AFTER = re.compile(r'\s{0,2}(?:' + '|'.join('(?P<s%d>%s)' % (i, pattern) for i, (pattern, _) in enumerate(SCALE_WORDS)) + ')', re.I)


def _scaled(value, tail):
    """(value * scale, scale word) when `tail` starts with a scale word, else (value, '')."""
    found = SCALE_AFTER.match(tail)
    if not found:
        return value, ''
    for index, (_, factor) in enumerate(SCALE_WORDS):
        if found.group('s%d' % index):
            return value * factor, found.group().strip()
    return value, ''


def numeric_tokens(text, strip_enumeration=False):
    text = str(text)
    if strip_enumeration:
        text = re.sub(r'(?m)^\s*[①-⑳]\s*', '', text)
    text = THOUSANDS.sub(lambda match: match.group().replace(',', ''), text)
    text = unicodedata.normalize('NFKC', text).replace('−', '-')
    if strip_enumeration:
        text = re.sub(r'(?m)^\s*\d+(?:\.(?!\d)\s*|[、)]\s*)', '', text)
        # Zero-padded step labels in a process diagram ("01 收运 → 02 破碎", "STEP 01") number
        # the steps; they are not quantities. Narrow on purpose: exactly 01-09, not part of a
        # longer number, date or time (2026-01-05, 08:30, 0.5), and followed by a Chinese word or
        # introduced by a step keyword. "03 mg/L" or "06%" stay real claims and stay checked.
        text = re.sub(r'(?<![\d.:/\-])0[1-9](?![\d.:/%\-])(?=\s*[一-鿿])', '', text)
        text = re.sub(r'(?i)\b(step|stage|phase|no\.?)\s*0[1-9](?![\d.:/%\-])', r'\1', text)
    text = re.sub(r'百分之\s*(\d+(?:\.\d+)?)', r'\1%', text)
    values = {}
    for match in NUMBER.finditer(text):
        try:
            value = Decimal(match.group('value'))
        except InvalidOperation:
            continue
        if value.is_finite():
            percent = '%' if match.group('percent') else ''
            label = match.group().strip()
            if not percent:
                value, word = _scaled(value, text[match.end():])
                if word:
                    label += word
            values.setdefault((value, percent), label)
    return values


def scaled_number_spans(text):
    """(start, end, value) for each number written with a scale word ("900万", "9 млн", "9 million").

    Lets a caller that compares numbers as strings accept a translated quantity when its VALUE
    equals a value present in the source. Percentages and bare numbers are never included."""
    spans = []
    text = str(text)
    for match in NUMBER.finditer(text):
        if match.group('percent'):
            continue
        try:
            value = Decimal(match.group('value'))
        except InvalidOperation:
            continue
        if not value.is_finite():
            continue
        found = SCALE_AFTER.match(text[match.end():])
        scaled, word = _scaled(value, text[match.end():])
        if word and found:
            spans.append((match.start(), match.end() + found.end(), scaled))
    return spans


def number_values(text):
    """Canonical numeric values present in `text` (scale words applied, % ignored)."""
    return {key[0] for key in numeric_tokens(text) if key[1] == ''}


def visible_texts(spec):
    """Only user-visible content, never evidence/source/layout metadata."""
    def strings(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            yield str(value)
        elif isinstance(value, list):
            for item in value: yield from strings(item)
        elif isinstance(value, dict):
            for key in ('text', 'title', 'heading', 'label', 'content', 'items', 'points', 'body'):
                if key in value: yield from strings(value[key])
    return [text for key in ('title', 'copy', 'points', 'body') for text in strings(spec.get(key))]


def prepare_numeric_draft(spec):
    """Mark the three observed design examples, never fabricate evidence.

    Only exact recognized occurrences are masked in a separate checking copy.
    Another quantity with the same value on the same page remains checked.
    The actual page retains its quantities and an explicit visible draft label.
    """
    guard = deepcopy(spec)
    annotations = {}
    copy = spec.get('copy', '')
    points = spec.get('points', [])
    proposed_grades = bool(re.search(r'(?:构建|拟议|建议|示例|拟定).{0,30}分级', copy))
    application_categories = all(any(isinstance(point, str) and label in point for point in points)
                                 for label in ('优先立项', '协同立项', '配套立项'))
    labels = {'reference_unit_example': '核算示例（待确认）',
              'illustrative_grade_labels': '拟议分级示例（待确认，非已发布标准）',
              'proposed_application_combination': '建议组合（数量待确认）'}

    def mark(kind, path, matches):
        record = annotations.setdefault(kind, {'kind': kind, 'status': 'requires_confirmation',
            'requires_user_confirmation': True, 'externally_verified': False,
            'visible_label': labels[kind], 'fields': [], 'numbers': []})
        if path not in record['fields']:record['fields'].append(path)
        for match in matches:
            for key in numeric_tokens(match.group()):
                token = str(key[0]) + key[1]
                if token not in record['numbers']:record['numbers'].append(token)

    def checking_value(value, path):
        if isinstance(value, str):
            spans = []
            factual_clause = bool(FACTUAL_AUTHORITY.search(value.replace('非已发布标准', '')))
            if not factual_clause and any(term in value for term in ('功能单位', '声明单位', '核算基准')):
                matches = list(REFERENCE_UNIT.finditer(value))
                if matches:
                    mark('reference_unit_example', path, matches)
                    spans.extend(match.span('quantity') for match in matches)
            if proposed_grades and not factual_clause and '划分' in value:
                matches = list(GRADE_LABELS.finditer(value))
                if matches:
                    mark('illustrative_grade_labels', path, matches)
                    spans.extend(match.span() for match in matches)
            if (application_categories and not factual_clause and re.search(r'(?:采取|建议|拟).{0,160}(?:立项|申报)组合', value)
                    and value.count('+') == 2):
                matches = list(ONE_APPLICATION.finditer(value))
                if len(matches) == 3:
                    mark('proposed_application_combination', path, matches)
                    spans.extend(match.span() for match in matches)
            masked = list(value)
            for start, end in spans:masked[start:end] = ' ' * (end - start)
            return ''.join(masked)
        if isinstance(value, list):return [checking_value(item, path + '[' + str(i) + ']') for i, item in enumerate(value)]
        if isinstance(value, dict):
            return {key: checking_value(item, path + '.' + key) if key in
                    ('text', 'title', 'heading', 'label', 'content', 'items', 'points', 'body') else item
                    for key, item in value.items()}
        return value

    for key in ('title', 'copy', 'points', 'body'):
        if key in guard:guard[key] = checking_value(guard[key], key)
    result = list(annotations.values())
    spec.pop('numeric_proposals', None)
    if result:
        prefix = '；'.join(record['visible_label'] for record in result) + '：'
        spec['copy'] = prefix + copy
        guard['copy'] = prefix + guard.get('copy', '')
        note = '\n设计草案说明：' + '；'.join(record['visible_label'] for record in result) + '。这些数字属于拟议核算示例、分级标签或申报组合，尚未经用户确认，不作为已核实的政策、标准或实测依据。'
        if len(spec.get('speaker_notes', '')) + len(note) <= 16000:
            spec['speaker_notes'] = spec.get('speaker_notes', '') + note
        spec['numeric_proposals'] = result
    return guard, result


def _reference_scope(text):
    # Reference identities cannot support measurements just because the digits
    # happen to match. Preserve existing thousands/separator behavior first.
    text = THOUSANDS.sub(lambda match: match.group().replace(',', ''), str(text))
    text = unicodedata.normalize('NFKC', text.replace(',', '、').replace('，', '、')).replace('−', '-')
    identifiers, spans = {}, []
    for match in STANDARD_ID.finditer(text):
        key = re.sub(r'\s+', '', match['family']).upper() + ':' + match['serial'] + ':' + match['year']
        identifiers[key] = match.group();spans.append(match.span())
    masked = list(text)
    for start, end in spans:masked[start:end] = ' ' * (end - start)
    return ''.join(masked), identifiers


def verify_numeric_claims(spec, grounded, materials, config, cancel_check=None, page_cache=None):
    claimed, claimed_references = {}, {}
    for text in visible_texts(spec):
        text, identifiers = _reference_scope(text)
        claimed.update(numeric_tokens(text, strip_enumeration=True));claimed_references.update(identifiers)
    grounded_text, grounded_references = _reference_scope(grounded)
    missing = set(claimed) - set(numeric_tokens(grounded_text))
    missing_references = set(claimed_references) - set(grounded_references)
    if not missing and not missing_references:return []
    cache = page_cache if page_cache is not None else {}
    reports = []
    cited = set(spec.get('evidence_ids') or [])
    for material in materials or []:
        if not isinstance(material, dict) or material.get('role') not in (None, '', 'material'): continue
        chunks = [c for c in material.get('chunks', []) if isinstance(c, dict) and c.get('id') in cited
                  and c.get('evidence_kind', 'project_material') == 'project_material']
        if not chunks: continue
        raw_path = material.get('path')
        if not raw_path and material.get('relative_path') and config.get('UPLOAD_FOLDER'):
            raw_path = Path(config['UPLOAD_FOLDER']) / material['relative_path']
        if not raw_path or Path(raw_path).suffix.lower() != '.pdf': continue
        expected = material.get('sha256')
        for chunk in chunks:
            check_cancel(cancel_check)
            source = chunk.get('source', {})
            if not isinstance(source, dict) or not isinstance(source.get('page'), int) or isinstance(source['page'], bool): continue
            if source.get('sha256') != expected or not re.fullmatch(r'[0-9a-f]{64}', str(expected or '')):
                raise PipelineError('material_hash_mismatch', '数字核对的原PDF与所引证据来源校验和不一致。')
            # Include the controlled path so two records cannot borrow another
            # file's successful page check, even when claiming the same SHA.
            key = (str(Path(raw_path).resolve()), expected, source['page'])
            if key not in cache:
                if len(cache) >= 12:
                    raise PipelineError('numeric_recheck_limit', '数字原页核对已达到12页上限，未放行尚未验证的主张。')
                from .wuyu_materials import read_verified_pdf_page
                cache[key] = read_verified_pdf_page(raw_path, config, expected, source['page'], cancel_check)
            page = cache[key]
            page_text, references = _reference_scope(page['text'])
            recovered = missing & set(numeric_tokens(page_text))
            recovered_references = missing_references & set(references)
            if recovered or recovered_references:
                reports.append({'slide_number': spec.get('number'), 'evidence_id': chunk['id'],
                    'material_id': material.get('id'), 'filename': str(material.get('filename') or source.get('filename') or Path(raw_path).name),
                    'source_sha256': expected, 'page': page['page'], 'page_text_sha256': page['page_text_sha256'],
                    'extractor': page['extractor'], 'recovered_numbers': sorted(claimed[value] for value in recovered),
                    **({'recovered_identifiers': sorted(recovered_references)} if recovered_references else {})})
                missing -= recovered;missing_references -= recovered_references
            if not missing and not missing_references:return reports
    # Name the numbers that have no evidence. The rule is unchanged (nothing is let
    # through); the user and the assistant just no longer have to guess which figure
    # on which page to check. Structured and numbers-only: no source text, no paths.
    error = PipelineError('unsupported_numeric_claim', '可见文案中的数字未能对应所引项目证据或同页原PDF，已拒绝生成。')
    error.diagnostics = {
        'page': spec.get('number'),
        'unsupported_numbers': (sorted(claimed[value] for value in missing)[:8] + sorted(missing_references)[:4]),
    }
    raise error
