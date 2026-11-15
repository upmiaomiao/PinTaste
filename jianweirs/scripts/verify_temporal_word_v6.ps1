$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$qaRoot = Join-Path $taskRoot 'outputs/task-temporal-v6-20261025'
$application = $null
$document = $null
$records = @()
try {
    $application = New-Object -ComObject Word.Application
    $application.Visible = $false
    $application.DisplayAlerts = 0
    try { $application.AutomationSecurity = 3 } catch { }
    $cases = Get-ChildItem -LiteralPath (Join-Path $qaRoot 'fixtures/runs') -Directory
    foreach ($case in $cases) {
        $source = Join-Path $case.FullName 'report.docx'
        $pdf = Join-Path $case.FullName 'report.pdf'
        $document = $application.Documents.Open($source, $false, $true, $false)
        $document.Repaginate()
        $pages = $document.ComputeStatistics(2)
        $document.ExportAsFixedFormat($pdf, 17)
        $document.Close(0)
        [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($document)
        $document = $null
        $records += @{ source=$source; pdf=$pdf; pages=$pages; pdf_bytes=(Get-Item -LiteralPath $pdf).Length }
    }
    @{ renderer='Word.Application PDF export'; files=$records; visual_qa_passed=$false; canonical_renderer_limitation='Existing isolated LibreOffice image lacks Writer import support' } | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $qaRoot 'word-render.json') -Encoding UTF8
    $records | ConvertTo-Json -Depth 5
}
finally {
    if ($null -ne $document) { $document.Close(0); [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($document) }
    if ($null -ne $application) { $application.Quit(); [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($application) }
}
