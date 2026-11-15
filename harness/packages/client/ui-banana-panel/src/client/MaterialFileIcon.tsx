/** File-format glyphs identify material cards without external image assets. */

/** @param filename - User file name. @returns its file-format glyph. */
export function MaterialFileIcon({ filename }: { filename: string }) {
  const extension = filename.split('.').at(-1)?.toLowerCase() ?? ''
  const kind = /^(docx?|rtf)$/.test(extension) ? 'word' : extension === 'pdf' ? 'pdf'
    : /^(xlsx?|csv|tsv)$/.test(extension) ? 'sheet' : /^(pptx?)$/.test(extension) ? 'slides'
      : /^(md|markdown)$/.test(extension) ? 'markdown' : /^(png|jpe?g|webp)$/.test(extension) ? 'image' : 'text'
  const badge = { word:'W', pdf:'PDF', sheet:'X', slides:'P', markdown:'MD', image:'', text:'TXT' }[kind]
  return <svg width="28" height="32" viewBox="0 0 28 32" fill="none" aria-hidden data-file-icon={kind}>
    <path d="M5 1.5h12L24 9v21H5Z" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" />
    <path d="M17 2v7h7" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" />
    {kind === 'sheet' ? <path d="M10 15h10v10H10Zm0 5h10M15 15v10" stroke="currentColor" strokeWidth="1.2" />
      : kind === 'slides' ? <path d="M10 14h10v9H10Zm5 9v3m-3 0h6" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
        : kind === 'image' ? <><path d="m8 25 5-7 3 4 3-5 3 8Z" stroke="currentColor" strokeLinejoin="round" /><circle cx="11" cy="13" r="1.5" fill="currentColor" /></>
          : <path d="M10 16h10M10 20h10M10 24h7" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />}
    {badge ? <><rect x="1" y="9" width={badge.length > 1 ? 17 : 12} height="12" rx="2" fill="currentColor" /><text x={badge.length > 1 ? 9.5 : 7} y="17.5" textAnchor="middle" fill="var(--dsw-alias-bg-base)" fontSize={badge.length > 2 ? 6 : 8} fontFamily="Arial, sans-serif" fontWeight="700">{badge}</text></> : null}
  </svg>
}
