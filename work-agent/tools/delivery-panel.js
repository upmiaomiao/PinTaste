function __wuyuDeliveryText(key) {
  const text = {
    title: ['选择输出格式', 'Choose an output format'], scope: ['文件交付', 'File delivery'],
    image: ['图片版 PPTX', 'Image-based PPTX'], editable: ['可编辑 PPTX', 'Editable PPTX'],
    pdf: ['PDF 文档', 'PDF document'], images: ['页面图片 ZIP', 'Slide images ZIP'],
    traceLater: ['全部页面审核后，在交付面板选择可编辑版，再开始转换和检查。', 'After reviewing all pages, choose editable output in the delivery panel to start conversion and checks.'],
    hint: ['选择后先放入对话，确认发送后处理。可编辑版需要额外转换，开始前核对调用上限与预计费用。', 'Your choice is drafted in the chat for confirmation. Editable output needs additional conversion; review the call limit and estimated cost before starting.'],
    waiting: ['审核完当前页面后，可选择输出格式。', 'Review the current pages before choosing an output format.'],
    imageRequest: ['请将当前已审核页面导出为图片版 PPTX，不重新生成页面。', 'Export the currently reviewed pages as an image-based PPTX without regenerating pages.'],
    editableRequest: ['请将当前已审核页面导出为可编辑 PPTX，保留现有图片；开始转换前先说明新增调用上限与预计费用。', 'Export the currently reviewed pages as an editable PPTX while retaining the images. Explain the additional call limit and estimated cost before starting conversion.'],
    pdfRequest: ['请将当前已审核页面导出为 PDF，不重新生成页面。', 'Export the currently reviewed pages as PDF without regenerating pages.'],
    imagesRequest: ['请将当前已审核页面打包为页面图片 ZIP，不重新生成页面。', 'Export the currently reviewed pages as a ZIP of slide images without regenerating pages.']
  };
  return text[key]?.[document.documentElement.lang === 'en' ? 1 : 0] ?? key;
}
function __wuyuDeliveryChoices(capabilities) {
  const ready = capabilities.image_pptx === true && !(m.snapshot.jobs || []).some(job => ['queued', 'running'].includes(job.status));
  return `<section data-delivery-formats><h3>${$(__wuyuDeliveryText('title'))}</h3><div style="display:flex;flex-wrap:wrap;gap:8px">${['image','editable','pdf','images'].map(kind => ready ? Ge(__wuyuDeliveryText('scope'), __wuyuDeliveryText(kind), __wuyuDeliveryText(kind+'Request')) : `<button type="button" class="detail-button" disabled>${$(__wuyuDeliveryText(kind))}</button>`).join('')}</div><p class="preview-note">${$(__wuyuDeliveryText(ready ? 'hint' : 'waiting'))}</p></section>`;
}
