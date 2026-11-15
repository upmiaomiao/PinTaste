/**
 * Presentation-only adapter for the RS model's small XML vocabulary.
 * Never mutates the supplied turn, executes markup, fetches entities, or repairs
 * coordinates. All returned strings must be rendered as React text nodes.
 */
import {remoteSensingHasMarkup} from './remoteSensingDisplay.mjs';
const RS_IDENTIFIERS = new Set([
  'wuyu_remote_sensing', 'wuyu-jianwei-rs',
  'wuyu-qwen35-9b-rs-rl750-xml', 'wuyu_qwen35_9b_rl750_xml',
]);
const ROLE_NAMES = {
  global_observation: '全局场景观察', direct_evidence: '疑似区域观察',
  alternative_check: '替代解释核对', context_risk: '周边环境与风险',
  decision_review: '综合研判复核',
};
const REVIEW_NAMES = {
  initial_model_judgement: '初步判断', strongest_alternative: '可能的替代解释',
  counter_evidence: '核对依据', missed_target_check: '其他区域复核',
  count_class_bbox_check: '数量、类别与范围核对', correction_action: '复核处理',
  initial_model_judgment: '初步判断', decision_review: '综合复核',
};
const BLOCK_NAMES = new Set(['thinking', 'answer']);
const UNSAFE_NAMES = new Set(['script', 'iframe', 'img', 'image', 'style', 'link', 'object', 'embed', 'svg', 'math']);
const LIMITS = { length: 1_000_000, nodes: 30_000, depth: 64 };

function valueText(value) { return typeof value === 'string' ? value : ''; }
function identifier(value) { return valueText(value).trim().toLowerCase(); }
function unique(values) { return [...new Set(values.filter(Boolean))]; }

export function isRemoteSensingTurn(turn = {}) {
  const profile = turn.model_profile || turn.modelProfile || turn.profile || {};
  const request = turn.request || {};
  return [turn.model_id, turn.modelId, turn.model_name, turn.modelName,
    turn.model, turn.profile_id, turn.profileId, profile.id, profile.model,
    request.model_id, request.model, request.model_name]
    .some(value => RS_IDENTIFIERS.has(identifier(value)));
}

function addIssue(issues, code, message, severity = 'warning') {
  if (!issues.some(issue => issue.code === code && issue.message === message)) {
    issues.push({ code, message, severity });
  }
}

function decodeEntities(text, issues) {
  return text.replace(/&([^;\s<&]+);/g, (all, name) => {
    const builtins = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'" };
    if (Object.hasOwn(builtins, name)) return builtins[name];
    const number = /^#x[\da-f]+$/i.test(name) ? parseInt(name.slice(2), 16)
      : /^#\d+$/.test(name) ? Number(name.slice(1)) : NaN;
    if (Number.isInteger(number) && number > 0 && number <= 0x10ffff
      && !(number >= 0xd800 && number <= 0xdfff)
      && (number >= 32 || [9, 10, 13].includes(number))) return String.fromCodePoint(number);
    addIssue(issues, 'invalid_entity', '输出含有不受支持的 XML 实体，原文已保留。', 'error');
    return all;
  });
}

function tagNameAt(text, pos) {
  const match = /^[A-Za-z_][\w.:-]*/.exec(text.slice(pos));
  return match ? match[0] : '';
}
function localName(name) { return name.split(':').pop().toLowerCase(); }
function whitespace(char) { return char !== undefined && /\s/.test(char); }

/** Tokenize a single tag with quote-aware boundaries, then parse its attrs. */
function readTag(text, start, issues) {
  let end = start + 1, quote = '';
  for (; end < text.length; end++) {
    const char = text[end];
    if (quote) { if (char === quote) quote = ''; }
    else if (char === '"' || char === "'") quote = char;
    else if (char === '>') break;
  }
  if (end >= text.length) return { error: 'partial_tag', end: text.length };
  let pos = start + 1;
  const closing = text[pos] === '/';
  if (closing) pos++;
  const name = tagNameAt(text, pos);
  if (!name) return { error: 'invalid_tag', end: end + 1 };
  pos += name.length;
  const attrs = {};
  let selfClosing = false;
  while (pos < end) {
    const hadSpace = whitespace(text[pos]);
    while (whitespace(text[pos])) pos++;
    if (pos === end) break;
    if (!closing && text[pos] === '/' && pos + 1 === end) { selfClosing = true; pos++; break; }
    if (closing || !hadSpace) return { error: 'invalid_tag', end: end + 1 };
    const attrName = tagNameAt(text, pos);
    if (!attrName) return { error: 'invalid_attribute', end: end + 1 };
    pos += attrName.length;
    while (whitespace(text[pos])) pos++;
    if (text[pos++] !== '=') return { error: 'invalid_attribute', end: end + 1 };
    while (whitespace(text[pos])) pos++;
    const attrQuote = text[pos++];
    if (attrQuote !== '"' && attrQuote !== "'") return { error: 'invalid_attribute', end: end + 1 };
    const attrStart = pos;
    while (pos < end && text[pos] !== attrQuote) pos++;
    if (pos >= end) return { error: 'invalid_attribute', end: end + 1 };
    if (Object.hasOwn(attrs, attrName)) return { error: 'duplicate_attribute', end: end + 1 };
    if (text.slice(attrStart, pos).includes('<')) return { error: 'invalid_attribute', end: end + 1 };
    attrs[attrName] = decodeEntities(text.slice(attrStart, pos++), issues);
  }
  if (UNSAFE_NAMES.has(localName(name)) || Object.keys(attrs).some(key => /^on/i.test(key))) {
    addIssue(issues, 'unsafe_markup', '输出包含非遥感格式的活动标记，仅保留为原文。', 'error');
  }
  return { name, local: localName(name), attrs, closing, selfClosing, end: end + 1 };
}

function parseBlock(text, start, issues) {
  const first = readTag(text, start, issues);
  if (first.error || first.closing) return { root: null, end: first.end, partial: first.error === 'partial_tag', error: first.error || 'invalid_root' };
  const root = { name: first.name, local: first.local, attrs: first.attrs, parts: [], complete: first.selfClosing };
  const stack = first.selfClosing ? [] : [root];
  let pos = first.end, nodes = 1;
  while (stack.length && pos < text.length) {
    if (text[pos] !== '<') {
      const next = text.indexOf('<', pos);
      const end = next === -1 ? text.length : next;
      const fragment = text.slice(pos, end);
      if (fragment.includes(']]>')) return { root, end, partial: false, error: 'invalid_cdata_end' };
      stack.at(-1).parts.push(decodeEntities(fragment, issues));
      pos = end;
      continue;
    }
    if (text.startsWith('<!--', pos)) {
      const end = text.indexOf('-->', pos + 4);
      if (end === -1) return { root, end: text.length, partial: true, error: 'partial_comment' };
      if (text.slice(pos + 4, end).includes('--')) return { root, end: end + 3, partial: false, error: 'invalid_comment' };
      pos = end + 3; continue;
    }
    if (text.startsWith('<![CDATA[', pos)) {
      const end = text.indexOf(']]>', pos + 9);
      if (end === -1) return { root, end: text.length, partial: true, error: 'partial_cdata' };
      stack.at(-1).parts.push(text.slice(pos + 9, end)); pos = end + 3; continue;
    }
    if (text.startsWith('<!', pos) || text.startsWith('<?', pos)) {
      return { root, end: text.length, partial: false, error: 'unsupported_declaration' };
    }
    const tag = readTag(text, pos, issues);
    if (tag.error) return { root, end: tag.end, partial: tag.error === 'partial_tag', error: tag.error };
    pos = tag.end;
    if (tag.closing) {
      if (tag.name !== stack.at(-1).name) return { root, end: pos, partial: false, error: 'mismatched_tag' };
      stack.pop().complete = true;
    } else {
      if (++nodes > LIMITS.nodes || stack.length >= LIMITS.depth) return { root, end: text.length, partial: false, error: 'parse_limit' };
      const child = { name: tag.name, local: tag.local, attrs: tag.attrs, parts: [], complete: tag.selfClosing };
      stack.at(-1).parts.push(child);
      if (!tag.selfClosing) stack.push(child);
    }
  }
  return { root, end: pos, partial: stack.length > 0, error: stack.length ? 'unclosed_tag' : null };
}

function unwrapWholeFence(text) {
  const match = text.match(/^\s*(`{3,}|~{3,})(?:xml)?[ \t]*\r?\n([\s\S]*?)\r?\n([`~]+)[ \t]*\s*$/i);
  if (match && match[1][0] === match[3][0] && match[3].length === match[1].length) return match[2];
  return text;
}

// A damaged analysis wrapper must not hide an independently closed final block.
// Tokenize boundaries to ignore literal tags inside comments, CDATA and attrs.
function independentFinalBlock(text, start, thinkingName, issues, allowPartial = false) {
  let pos = start;
  while (pos < text.length) {
    pos = text.indexOf('<', pos);
    if (pos < 0) return null;
    if (text.startsWith('<!--', pos) || text.startsWith('<![CDATA[', pos)) {
      const comment = text.startsWith('<!--', pos), ending = comment ? '-->' : ']]>';
      const end = text.indexOf(ending, pos + (comment ? 4 : 9));
      if (end < 0) return null;
      pos = end + ending.length;
      continue;
    }
    if (text.startsWith('<!', pos) || text.startsWith('<?', pos)) return null;
    const tag = readTag(text, pos, issues);
    if (tag.error) return null;
    pos = tag.end;
    if (!tag.closing || tag.name !== thinkingName) continue;
    const next = pos + (text.slice(pos).match(/^\s*/)?.[0].length || 0);
    if (text[next] !== '<') return null;
    const first = readTag(text, next, issues);
    if (first.error || first.closing || first.local !== 'answer') return null;
    const parsed = parseBlock(text, next, issues);
    return (!parsed.error && parsed.root?.complete) || (allowPartial && parsed.partial && parsed.root)
      ? {start:next,...parsed} : null;
  }
  return null;
}

function scanSource(raw, issues, active = false) {
  const text = unwrapWholeFence(raw);
  const blocks = [], remaining = [], streamRemaining = [];
  let pos = 0, partial = false, invalid = false, recoveredFinal = false;
  if (text.length > LIMITS.length) {
    addIssue(issues, 'parse_limit', '输出较长，结构化展示未完成；请查看完整原文。', 'error');
    return { blocks, remaining: raw, streamRemaining: '', partial, invalid: true };
  }
  const trigger = /<(?:[A-Za-z_][\w.-]*:)?(thinking|answer)(?=[\s/>])/ig;
  while (pos < text.length) {
    trigger.lastIndex = pos;
    const match = trigger.exec(text);
    if (!match) { remaining.push(text.slice(pos)); streamRemaining.push(text.slice(pos)); break; }
    remaining.push(text.slice(pos, match.index));
    streamRemaining.push(text.slice(pos, match.index));
    const parsed = parseBlock(text, match.index, issues);
    if (parsed.root) blocks.push(parsed.root);
    partial ||= parsed.partial;
    if (parsed.error) {
      if (!parsed.partial) invalid = true;
      addIssue(issues, parsed.partial ? 'incomplete_xml' : 'malformed_xml',
        parsed.partial ? '结构化输出尚未完整，已保留原文；暂不生成影像标注。'
          : '结构化输出存在格式错误，已保留原文；暂不生成影像标注。', parsed.partial ? 'warning' : 'error');
      const final = parsed.root?.local === 'thinking' && !parsed.partial
        ? independentFinalBlock(text, parsed.end, parsed.root.name, issues, active) : null;
      if (final) {
        // The damaged wrapper remains verbatim in rawSources, while completed
        // analysis sections and extra text are shown through their parsed fields.
        blocks.push(final.root);
        recoveredFinal = true;
        pos = final.end;
        continue;
      }
      remaining.push(text.slice(match.index));
      break;
    }
    pos = parsed.end;
  }
  if (blocks.length && /<!\s*(?:DOCTYPE|ENTITY)\b/i.test(text)) {
    addIssue(issues, 'external_entities_forbidden', '输出包含实体声明，结构化展示已停用。', 'error');
    invalid = true;
  }
  const remainingText = remaining.join('').trim();
  if (blocks.length && /<\/(?:[A-Za-z_][\w.-]*:)?(?:thinking|answer)(?=[\s>])/i.test(remainingText)) {
    addIssue(issues, 'unmatched_wrapper', '输出含有无法对应的结束标记，已保留原文；暂不生成影像标注。', 'error');
    invalid = true;
  }
  // A fence surrounding a schema block with ordinary prefix/suffix remains visible
  // in the fallback rather than being silently consumed.
  return { blocks, remaining: remainingText, streamRemaining: streamRemaining.join('').trim(), partial, invalid, recoveredFinal };
}

function children(node, name) { return node.parts.filter(part => typeof part !== 'string' && (!name || part.local === name)); }
function content(node) { return node.parts.map(part => typeof part === 'string' ? part : content(part)).join('').trim(); }
function fields(node, name) { return children(node, name).filter(child => child.complete).map(content); }
function field(node, name) { return fields(node, name)[0] || ''; }
function attr(node, name) {
  return Object.entries(node.attrs).find(([key]) => localName(key) === name)?.[1] || '';
}

function decisionKind(text) {
  const compact = text.replace(/\s/g, '');
  if (!compact) return 'unknown';
  if (/^(?:未发现|未见|无|不存在|未检测到)(?:明显|疑似|异常)?(?:固废|固体废物)(?:堆存|堆放|堆积|倾倒)?[。.!！]?$/.test(compact)
    || /^(?:无固废|未发现固废堆存|未见固废堆存)$/.test(compact)) return 'negative';
  if (/^(?:存在|发现|发现了|检测到|疑似存在|存在疑似)(?:疑似)?(?:固废|固体废物)(?:堆存|堆放|堆积|倾倒)[。.!！]?$/.test(compact)) return 'positive';
  if (/不确定|无法|难以|不能|待核实|需核实|尚不|疑似|可能/.test(compact)) return 'uncertain';
  return 'unknown';
}

function imagesOf(turn) {
  const files = Array.isArray(turn.annotationImages) ? turn.annotationImages
    : Array.isArray(turn.files) ? turn.files : [];
  return files.filter(file => file && (file.kind === 'image' || valueText(file.mime_type || file.mimeType || file.type).startsWith('image/')
    || (Array.isArray(turn.annotationImages) && typeof file.id === 'string')))
    .map(file => ({ ...file, id: valueText(file.id || file.file_id || file.fileId) })).filter(file => file.id);
}

function bindRegion(node, images, turn, issues) {
  const refs = unique(['image_id', 'file_id', 'image_ref', 'file_ref'].flatMap(name => [attr(node, name), ...fields(node, name)]));
  const indexes = unique([attr(node, 'image_index'), ...fields(node, 'image_index')]);
  const declaredRef = refs[0] || (indexes[0] ? `image_index:${indexes[0]}` : '');
  if (turn.annotationImageBindingAmbiguous) return { imageRef: declaredRef, fileId: '', imageBinding: 'ambiguous' };
  if (refs.length > 1 || indexes.length > 1) {
    addIssue(issues, 'conflicting_image_reference', '目标包含相互冲突的影像引用，暂不叠加到影像。');
    return { imageRef: declaredRef, fileId: '', imageBinding: 'ambiguous' };
  }
  let matched = refs.length ? images.find(image => image.id === refs[0]) : null;
  if (indexes.length) {
    if (turn.annotationImageIndexBase !== 1 || !/^[1-9]\d*$/.test(indexes[0])) {
      addIssue(issues, 'unverified_image_index', '影像序号的对应关系尚未确认，暂不叠加到影像。');
      return { imageRef: declaredRef, fileId: '', imageBinding: 'ambiguous' };
    }
    const indexed = images[Number(indexes[0]) - 1];
    if (refs.length && (!indexed || indexed.id !== matched?.id)) {
      addIssue(issues, 'conflicting_image_reference', '目标的影像引用与序号不一致，暂不叠加到影像。');
      return { imageRef: declaredRef, fileId: '', imageBinding: 'ambiguous' };
    }
    matched = indexed;
  }
  if (refs.length || indexes.length) return { imageRef: declaredRef, fileId: matched?.id || '', imageBinding: matched ? 'explicit' : 'missing' };
  if (images.length === 1) return { imageRef: '', fileId: images[0].id, imageBinding: 'single' };
  return { imageRef: '', fileId: '', imageBinding: images.length ? 'ambiguous' : 'missing' };
}

function parseRegion(node, index, images, turn, issues) {
  const id = attr(node, 'id') || `R${index + 1}`, label = field(node, 'label');
  const bboxNodes = children(node, 'bbox').filter(child => child.complete);
  const bboxNode = bboxNodes[0];
  const coord = bboxNode ? attr(bboxNode, 'coord') : '';
  const rawBbox = bboxNode ? content(bboxNode) : '';
  let bbox = null;
  try { const parsed = JSON.parse(rawBbox); if (Array.isArray(parsed)) bbox = parsed; } catch { /* raw survives in rawSources */ }
  const valid = bboxNodes.length === 1 && coord === 'normalized_1000' && bbox?.length === 4
    && bbox.every(number => Number.isInteger(number) && number >= 0 && number <= 1000)
    && bbox[0] < bbox[2] && bbox[1] < bbox[3];
  if (!valid) addIssue(issues, 'invalid_bbox', `目标 ${id} 的坐标格式或范围不可靠，暂不生成标注框。`);
  if (fields(node, 'label').length > 1) addIssue(issues, 'multiple_region_labels', `目标 ${id} 含有多个类型标签，原文已保留。`);
  const binding = bindRegion(node, images, turn, issues);
  if (binding.imageBinding === 'ambiguous') addIssue(issues, 'ambiguous_image_binding', '多个影像或历史影像的对应关系不明确，目标已列出，暂不自动叠加。');
  if (binding.imageBinding === 'missing') addIssue(issues, 'missing_image_binding', '未找到目标对应的已上传影像，暂不自动叠加。');
  return { id, label, bbox: valid ? bbox : null, rawBbox, coord, valid: Boolean(valid), ...binding, overlayEligible: false };
}

// Nested decision_review wrappers occur in this model's trained output. They
// are review prose, not an extra schema field or a second model conclusion.
function extractReview(node, target, allowPartial = false, textOf = content) {
  const own = node.parts.filter(part => typeof part === 'string').join('').trim();
  if ((node.complete || allowPartial) && own) {
    const text = textOf({ ...node, parts: node.parts.filter(part => typeof part === 'string') })
      .replace(/。\s*[;；]\s*/g, '。\n\n');
    if (text) target.push({ key: node.local, label: REVIEW_NAMES[node.local] || '复核记录', text });
  }
  for (const child of children(node)) {
    if (child.local === 'decision_review') extractReview(child, target, allowPartial, textOf);
    else if ((child.complete || allowPartial) && textOf(child)) {
      target.push({ key: child.local, label: REVIEW_NAMES[child.local] || '复核记录', text: textOf(child) });
    }
  }
}

function extractThinking(root, result, unsupported) {
  const ownText = root.parts.filter(part => typeof part === 'string').join('').trim();
  if (ownText) unsupported.push(ownText);
  for (const child of children(root)) {
    if (child.local === 'section') {
      const role = attr(child, 'role');
      const texts = fields(child, 'reasoning');
      const own = child.parts.filter(part => typeof part === 'string').join('').trim();
      const text = unique([...texts, own]).join('\n\n');
      if (text && child.complete) result.sections.push({ name: attr(child, 'name') || ROLE_NAMES[role] || '影像观察', role, text });
      for (const extra of children(child).filter(node => node.local !== 'reasoning')) {
        if (extra.local === 'decision_review') extractReview(extra, result.review);
        else if (content(extra)) unsupported.push(content(extra));
      }
    } else if (child.local === 'decision_review') {
      extractReview(child, result.review);
    } else if (content(child)) unsupported.push(content(child));
  }
}

function parseAnswer(root, result, images, turn, unsupported) {
  const known = new Set(['decision', 'risk_level', 'regions', 'confidence', 'conclusion']);
  for (const child of children(root)) if (!known.has(child.local) && content(child)) unsupported.push(content(child));
  const own = root.parts.filter(part => typeof part === 'string').join('').trim();
  if (own) unsupported.push(own);
  const decision = field(root, 'decision'), conclusion = field(root, 'conclusion');
  const confidenceText = field(root, 'confidence');
  const confidenceNumber = confidenceText && /^(?:0(?:\.\d+)?|1(?:\.0+)?)$/.test(confidenceText) ? Number(confidenceText) : null;
  if (confidenceText && confidenceNumber === null) addIssue(result.issues, 'invalid_confidence', '模型评分格式不可靠，原值已保留。');
  const regions = children(root, 'regions').flatMap(node => children(node, 'region'))
    .filter(node => node.complete).map((node, index) => parseRegion(node, index, images, turn, result.issues));
  for (const node of children(root, 'regions')) {
    const extra = node.parts.filter(part => typeof part === 'string' && part.trim()).join('').trim();
    if (extra) unsupported.push(extra);
    for (const child of children(node).filter(part => part.local !== 'region')) if (content(child)) unsupported.push(content(child));
  }
  for (const node of children(root, 'regions').flatMap(node => children(node, 'region'))) {
    for (const child of children(node).filter(part => !['label', 'bbox', 'image_id', 'file_id', 'image_ref', 'file_ref', 'image_index'].includes(part.local))) {
      if (content(child)) unsupported.push(content(child));
    }
  }
  for (const fieldName of ['decision', 'risk_level', 'confidence', 'conclusion']) {
    if (unique(fields(root, fieldName)).length > 1) addIssue(result.issues, 'conflicting_answer_fields', '同一结果包含相互不同的结论字段，暂不生成影像标注。', 'error');
  }
  if (root.complete && (!decision || !conclusion)) addIssue(result.issues, 'missing_answer_fields', '结构化结果缺少判断或结论，原文已保留。', 'error');
  return { decision, decisionKind: decisionKind(decision), riskLevel: field(root, 'risk_level'),
    confidence: confidenceNumber, confidenceText, conclusion, regions, complete: root.complete };
}

// A streaming presentation is an independent view of already received prose.
// It neither closes XML tags nor makes an unfinished answer eligible for boxes.
function safeStreamText(value) {
  const temporaryIssues = [];
  return decodeEntities(valueText(value), temporaryIssues)
    .replace(/^[ \t]*[`~]{1,}[^\r\n]*(?:\r?\n|$)/gm, '')
    .replace(/<!--[^]*?(?:-->|$)|<!\[CDATA\[[^]*?(?:\]\]>|$)/g, '')
    .replace(/<\/?[A-Za-z_][^>]*>|<![^>]*>|<\?[^>]*>/g, '')
    .replace(/<(?:[!?\/]?[A-Za-z_][^>]*|[!?\/]?)$/g, '')
    .replace(/&(?:#(?:x[\da-f]*|\d*)|[A-Za-z_][\w.:-]*);?/gi, '')
    .replace(/&$/g, '')
    .replace(/[\uD800-\uDBFF]$/g, '')
    .trimEnd();
}

function streamContent(node) {
  if (UNSAFE_NAMES.has(node.local)) return '';
  return safeStreamText(node.parts.map(part => typeof part === 'string' ? part : streamContent(part)).join('')).trim();
}

function ordinaryStreamText(value) {
  let text = valueText(value);
  // Remaining raw fallback may include a tag that has not finished arriving.
  // Exclude that tail rather than displaying its XML vocabulary or attributes.
  const boundary = text.search(/<(?:\/?[A-Za-z_]|[!?]|\/?$)/);
  if (boundary >= 0) text = text.slice(0, boundary);
  return safeStreamText(text).trim();
}

function activePresentation(blocks, answerSource, reasoningSource) {
  const stream = { sections: [], review: [], conclusion: '',
    ordinaryReasoning: ordinaryStreamText(reasoningSource.streamRemaining),
    ordinaryAnswer: ordinaryStreamText(answerSource.streamRemaining) };
  const conclusions = [];
  for (const block of blocks) {
    if (block.local === 'thinking') {
      for (const child of children(block)) {
        if (child.local === 'section') {
          const role = attr(child, 'role');
          const own = safeStreamText(child.parts.filter(part => typeof part === 'string').join('')).trim();
          const text = unique([...children(child, 'reasoning').map(streamContent), own]).join('\n\n');
          if (text) stream.sections.push({ name: safeStreamText(attr(child, 'name')).trim() || ROLE_NAMES[role] || '影像观察', role, text });
          for (const review of children(child, 'decision_review')) extractReview(review, stream.review, true, streamContent);
        } else if (child.local === 'decision_review') extractReview(child, stream.review, true, streamContent);
      }
    } else if (block.local === 'answer') {
      for (const conclusion of children(block, 'conclusion')) if (streamContent(conclusion)) conclusions.push(streamContent(conclusion));
    }
  }
  stream.sections = stream.sections.filter((section, index, list) => list.findIndex(other => other.name === section.name && other.role === section.role && other.text === section.text) === index);
  stream.review = stream.review.filter((review, index, list) => list.findIndex(other => other.key === review.key && other.text === review.text) === index);
  stream.conclusion = unique(conclusions)[0] || '';
  return stream;
}

/**
 * Result contract is deliberately independent of React or browser DOMParser.
 * annotationImages must list actual vision inputs when they differ from files.
 * annotationImageIndexBase=1 is opt-in for a verified 1-based prompt contract.
 */
export function parseRemoteSensingTurn(turn = {}) {
  const rawSources = { answer: valueText(turn.answer), reasoning: valueText(turn.reasoning) };
  const active = ['queued', 'running', 'streaming'].includes(turn.status);
  const result = { isRemoteSensing: isRemoteSensingTurn(turn), hasStructured: false, status: 'none',
    sections: [], review: [], answer: null, regions: [], overlayRegions: [], issues: [],
    ordinaryNonXmlReasoning: rawSources.reasoning, remainingAnswerText: rawSources.answer,
    additionalText: [], generationIncomplete: turn.status === 'incomplete', rawSources, stream: null };
  if (!result.isRemoteSensing) return result;
  const answerSource = scanSource(rawSources.answer, result.issues, active);
  const reasoningSource = scanSource(rawSources.reasoning, result.issues, active);
  result.recoveredFinalAfterMalformedAnalysis = !!(answerSource.recoveredFinal || reasoningSource.recoveredFinal);
  const blocks = [...answerSource.blocks, ...reasoningSource.blocks];
  result.hasStructured = blocks.length > 0;
  if (active) {
    result.stream = activePresentation(blocks, answerSource, reasoningSource);
    result.status = 'partial';
  }
  if (!result.hasStructured) return result;
  if (result.generationIncomplete) addIssue(result.issues, 'generation_incomplete',
    '本轮生成未完整结束，当前展示已收到的模型结果。');
  const images = imagesOf(turn), unsupported = [];
  result.ordinaryNonXmlReasoning = reasoningSource.remaining;
  result.remainingAnswerText = answerSource.remaining;
  const answers = [];
  for (const block of blocks) {
    if (block.local === 'thinking') extractThinking(block, result, unsupported);
    if (block.local === 'answer') answers.push(parseAnswer(block, result, images, turn, unsupported));
  }
  result.sections = result.sections.filter((section, index, list) => list.findIndex(other => other.name === section.name && other.role === section.role && other.text === section.text) === index);
  result.review = result.review.filter((review, index, list) => list.findIndex(other => other.key === review.key && other.text === review.text) === index);
  const completeAnswers = answers.filter(answer => answer.complete);
  result.answer = completeAnswers[0] || answers[0] || null;
  const signature = answer => JSON.stringify([answer.decision, answer.riskLevel, answer.confidenceText, answer.conclusion,
    answer.regions.map(region => [region.id, region.label, region.rawBbox, region.coord, region.imageRef])]);
  if (unique(completeAnswers.map(signature)).length > 1) {
    addIssue(result.issues, 'conflicting_answers', '输出包含相互不同的结构化结论，全部原文已保留，暂不生成影像标注。', 'error');
  }
  result.regions = result.answer?.regions || [];
  const ids = result.regions.map(region => region.id);
  if (unique(ids).length !== ids.length) addIssue(result.issues, 'duplicate_region_ids',
    '目标编号重复，目标归属不可靠，暂不生成影像标注。', 'error');
  if (result.answer?.decisionKind === 'negative' && result.regions.length) {
    addIssue(result.issues, 'negative_with_regions', '模型判断未发现堆存，但仍返回目标坐标，结果存在矛盾，暂不生成影像标注。', 'error');
  } else if (result.answer && !['positive', 'negative'].includes(result.answer.decisionKind)) {
    addIssue(result.issues, 'unconfirmed_decision', '模型尚未给出明确的堆存判断，目标坐标仅保留在结果列表。');
  }
  if (result.answer?.decisionKind === 'positive' && !result.regions.length) {
    addIssue(result.issues, 'positive_without_regions', '模型提示存在疑似堆存，但未提供可标注的目标范围。');
  }
  result.additionalText = unique(unsupported);
  const invalid = answerSource.invalid || reasoningSource.invalid || result.issues.some(issue => issue.severity === 'error');
  const partial = answerSource.partial || reasoningSource.partial || active
    || (result.answer && !result.answer.complete);
  result.status = active ? 'partial' : invalid ? 'invalid' : partial ? 'partial' : 'complete';
  for (const region of result.regions) {
    region.overlayEligible = result.status === 'complete' && result.answer?.decisionKind === 'positive'
      && region.valid && ['single', 'explicit'].includes(region.imageBinding);
  }
  result.overlayRegions = result.regions.filter(region => region.overlayEligible);
  return result;
}

export function remoteSensingSummary(parsed) {
  if (!parsed?.answer) return '';
  const answer = parsed.answer;
  const decision = answer.decisionKind === 'positive' ? '模型判断：发现疑似固废堆存。'
    : answer.decisionKind === 'negative' ? '模型判断：未发现固废堆存。'
      : `模型判断：${answer.decision || '尚无完整结果'}。`;
  const risk = answer.riskLevel ? `模型风险等级：${answer.riskLevel}。` : '';
  return `${decision}${risk}`;
}

export function remoteSensingScoreText(answer) {
  return answer?.confidenceText ? `模型评分 ${answer.confidenceText}（未校准，不代表实际概率）` : '';
}

export function remoteSensingReadableText(parsed) {
  const readable = text => remoteSensingHasMarkup(text) ? '' : text;
  if (!parsed?.hasStructured) return parsed?.isRemoteSensing ? readable(parsed.rawSources?.answer || '') : parsed?.rawSources?.answer || '';
  const paragraphs = [];
  const ordinary = readable(parsed.ordinaryNonXmlReasoning);
  if (parsed.sections.length || parsed.review.length || ordinary) {
    paragraphs.push('影像分析过程');
    for (const section of parsed.sections) paragraphs.push(`${section.name}\n${section.text}`);
    for (const review of parsed.review) paragraphs.push(`${review.label}：${review.text}`);
    if (ordinary) paragraphs.push(ordinary);
  }
  if (parsed.answer?.complete) {
    paragraphs.push('分析结论', remoteSensingSummary(parsed));
    if (parsed.answer.conclusion) paragraphs.push(parsed.answer.conclusion);
    for (const region of parsed.regions) {
      const coord = region.valid ? `，范围 ${JSON.stringify(region.bbox)}（normalized_1000）` : '，范围尚不可可靠标注';
      paragraphs.push(`疑似目标 ${region.id}：模型类型 ${region.label || '未提供'}${coord}。`);
    }
    if (parsed.answer.confidenceText) paragraphs.push(remoteSensingScoreText(parsed.answer));
  }
  if (readable(parsed.remainingAnswerText)) paragraphs.push(parsed.remainingAnswerText);
  paragraphs.push(...parsed.additionalText.map(readable));
  return paragraphs.filter(Boolean).join('\n\n');
}

export const REMOTE_SENSING_IDENTIFIERS = Object.freeze([...RS_IDENTIFIERS]);
