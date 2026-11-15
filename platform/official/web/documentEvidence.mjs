const imageName = /\.(?:png|jpe?g|webp|gif|bmp)$/i;

export function documentSourcesForTurn(turn = {}) {
  const files = turn.files || [];
  return (turn.documentSources || []).filter(source => {
    const file = files.find(item => item.id === source.file_id);
    const kind = source.file_kind || file?.kind;
    if (kind) return kind === 'document';
    // Older turns did not label the source file kind. Standalone image OCR
    // has no document page; scanned PDF pages remain document evidence.
    if (imageName.test(source.title || '')) return false;
    return !(source.modality === 'image' && !source.page);
  });
}

export function hasDocumentRetrieval(turn = {}) {
  return (turn.documentRag?.document_file_count || 0) > 0
    || (turn.files || []).some(file => file.kind === 'document')
    || documentSourcesForTurn(turn).length > 0;
}
