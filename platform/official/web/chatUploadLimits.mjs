/** Client preflight uses the chat API's binary size limits; the server remains authoritative. */
export const MIB = 1048576;
const imageExtensions = new Set(['.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp']);
const issue = (message, values) => ({message, ...(values ? {values} : {})});

function extension(name) {
  // Match the attachment service's basename, control-character and length rules.
  const cleaned = String(name ?? '').replace(/\\/g, '/').split('/').at(-1)
    .replace(/[\x00-\x1f]/g, '');
  const basename = Array.from(cleaned).slice(0, 160).join('');
  const dot = basename.lastIndexOf('.');
  return dot > 0 && dot < basename.length - 1 ? basename.slice(dot).toLowerCase() : '';
}

export function chatUploadIssue(config, existing, incoming) {
  if (!config || !Number.isSafeInteger(config.max_files) || config.max_files < 1 ||
      !['file_mb', 'image_mb', 'total_mb'].every(key => Number.isFinite(config[key]) && config[key] > 0) ||
      !Array.isArray(existing) || !Array.isArray(incoming)) {
    return issue('上传配置尚未就绪，请刷新后重试。');
  }
  if (existing.length + incoming.length > config.max_files) {
    return issue('每轮最多 {v0} 个文件。', {v0: config.max_files});
  }
  const all = [...existing, ...incoming];
  if (all.some(file => !file || !Number.isSafeInteger(file.size) || file.size < 0)) {
    return issue('无法读取附件大小，请重新选择文件。');
  }
  if (all.reduce((sum, file) => sum + file.size, 0) > config.total_mb * MIB) {
    return issue('每轮附件合计不能超过 {v0} MiB。', {v0: config.total_mb});
  }
  for (const file of incoming) {
    if (file.size === 0) return issue('文件“{v0}”为空，请选择其他文件。', {v0: file.name});
    const image = imageExtensions.has(extension(file.name));
    const limit = image ? config.image_mb : config.file_mb;
    if (file.size > limit * MIB) {
      return issue(image ? '图片“{v0}”不能超过 {v1} MiB。' : '文档“{v0}”不能超过 {v1} MiB。',
        {v0: file.name, v1: limit});
    }
  }
  return null;
}
