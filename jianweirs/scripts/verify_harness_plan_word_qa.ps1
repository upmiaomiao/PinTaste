$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$qaRoot = Join-Path $taskRoot 'outputs/wuyu-harness-v3-20261024'
$source = @(Get-ChildItem -LiteralPath $qaRoot -Filter '*.docx')
if ($source.Count -ne 1) { throw 'Expected exactly one design plan DOCX.' }
$render = Join-Path $qaRoot 'render'
[void](New-Item -ItemType Directory -Path $render -Force)
$pdf = Join-Path $render 'design-plan.pdf'
$stagedSource = Join-Path $render 'design-plan-input.docx'
Copy-Item -LiteralPath $source[0].FullName -Destination $stagedSource -Force
$application = $null
$document = $null
try {
    $application = New-Object -ComObject Word.Application
    $application.Visible = $false
    $application.DisplayAlerts = 0
    try { $application.AutomationSecurity = 3 } catch { }
    Write-Output 'Opening generated design plan'
    $document = $application.Documents.Open([string]$stagedSource, $false, $true, $false)
    $document.Repaginate()
    Write-Output 'Exporting design plan PDF'
    $document.ExportAsFixedFormat([string]$pdf, 17)
    $pages = $document.ComputeStatistics(2)
    $document.Close(0)
    [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($document)
    $document = $null
    @{ renderer='Word.Application PDF export'; pages=$pages; pdf_bytes=(Get-Item -LiteralPath $pdf).Length } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $qaRoot 'word-render.json') -Encoding UTF8
    @{ pages=$pages; pdf_bytes=(Get-Item -LiteralPath $pdf).Length } | ConvertTo-Json
}
finally {
    if ($null -ne $document) { $document.Close(0); [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($document) }
    if ($null -ne $application) { $application.Quit(); [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($application) }
}
