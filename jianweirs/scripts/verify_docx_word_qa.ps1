$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$qaRoot = Join-Path $taskRoot 'outputs/docx-export-20261024'
$items = @(
    @{ Source = 'points/site-01/report.docx'; Output = 'render-single/report.pdf' },
    @{ Source = 'batch-report.docx'; Output = 'render-batch/batch-report.pdf' }
)
$application = $null
$document = $null
$records = @()
try {
    $application = New-Object -ComObject Word.Application
    $application.Visible = $false
    $application.DisplayAlerts = 0
    try { $application.AutomationSecurity = 3 } catch { }
    foreach ($item in $items) {
        $source = [System.IO.Path]::GetFullPath((Join-Path $qaRoot $item.Source))
        $pdf = [System.IO.Path]::GetFullPath((Join-Path $qaRoot $item.Output))
        [void](New-Item -ItemType Directory -Path (Split-Path -Parent $pdf) -Force)
        $document = $application.Documents.Open($source, $false, $true, $false)
        $document.Repaginate()
        $pages = $document.ComputeStatistics(2)
        $document.ExportAsFixedFormat($pdf, 17)
        $document.Close(0)
        [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($document)
        $document = $null
        $records += @{ source=$item.Source; pdf=$item.Output; pages=$pages; pdf_bytes=(Get-Item -LiteralPath $pdf).Length }
    }
    @{ renderer='Word.Application PDF export'; files=$records; visual_qa_passed=$false } | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $qaRoot 'word-render.json') -Encoding UTF8
    $records | ConvertTo-Json -Depth 5
}
finally {
    if ($null -ne $document) { $document.Close(0); [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($document) }
    if ($null -ne $application) { $application.Quit(); [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($application) }
}
