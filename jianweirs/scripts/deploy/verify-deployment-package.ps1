[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$Path,
    [string]$ReportPath
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$projectDir = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..')).TrimEnd('\')
$zipPath = (Resolve-Path -LiteralPath $Path).Path
$needles = New-Object System.Collections.Generic.List[string]

function Add-PrivateNeedle([string]$Value) {
    if (-not [string]::IsNullOrWhiteSpace($Value) -and $Value.Length -ge 8 -and
        $Value -notmatch 'REPLACE|CHANGE_ME|GENERATED_LOCALLY') {
        $needles.Add($Value)
    }
}

try {
    # Actual secrets are compared only in memory. Neither values nor hashes of
    # low-entropy PSKs are written to reports or console output.
    foreach ($name in @('.env','.worker.env')) {
        $configPath = Join-Path $projectDir $name
        if (Test-Path -LiteralPath $configPath -PathType Leaf) {
            foreach ($line in Get-Content -LiteralPath $configPath -Encoding UTF8) {
                if ($line -match '^\s*[A-Z0-9_]*(?:API_KEY|TOKEN|PASSWORD|SECRET|PSK)\s*=\s*(.+)$') {
                    Add-PrivateNeedle ($Matches[1].Trim().Trim([char]39,[char]34))
                }
            }
        }
    }
    $clusterPath = Join-Path $projectDir 'cluster.json'
    if (Test-Path -LiteralPath $clusterPath -PathType Leaf) {
        $cluster = Get-Content -LiteralPath $clusterPath -Raw -Encoding UTF8 | ConvertFrom-Json
        foreach ($item in @($cluster.nodes) + @($cluster.clients)) { Add-PrivateNeedle ([string]$item.api_key) }
        $cluster = $null
    }
    $ngrokPath = Join-Path $projectDir 'ngrok.yml'
    if (Test-Path -LiteralPath $ngrokPath -PathType Leaf) {
        foreach ($line in Get-Content -LiteralPath $ngrokPath -Encoding UTF8) {
            if ($line -match '^\s*authtoken:\s*(\S+)\s*$') { Add-PrivateNeedle $Matches[1] }
        }
    }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [IO.Compression.ZipFile]::OpenRead($zipPath)
    try {
        $names = @($archive.Entries | ForEach-Object { $_.FullName.Replace('\','/') })
        $forbidden = @($names | Where-Object {
            $_ -match '(^|/)(\.env|\.worker\.env|cluster\.json|ngrok\.yml|\.venv|outputs|cache|tools|__pycache__|\.git)(/|$)' -or
            $_ -match '\.(png|jpg|jpeg|tif|tiff|exe|pyc|log|pid)$'
        })
        if ($forbidden.Count) { throw '部署ZIP含有私有配置、运行目录、可执行程序或影像；验证不通过。' }
        foreach ($name in @('requirements.txt','start-api.ps1','stop-api.ps1','setup-gateway.ps1',
                            'start-gateway.ps1','stop-gateway.ps1','install-ngrok.ps1','setup-ngrok.ps1',
                            'start-ngrok.ps1','stop-ngrok.ps1','CLOUD_DEPLOYMENT.md','cluster.example.json',
                            'omap_api/gateway.py','omap_api/gateway_admin.py','scripts/deploy/setup-worker.ps1')) {
            if ($names -notcontains $name) { throw ('部署ZIP缺少公开运行文件：' + $name) }
        }
        foreach ($entry in $archive.Entries) {
            if ($entry.FullName.Replace('\','/').StartsWith('wheels/')) { continue }
            $stream = $entry.Open()
            $reader = [IO.StreamReader]::new($stream,[Text.Encoding]::UTF8)
            try {
                $content = $reader.ReadToEnd()
                foreach ($needle in $needles) {
                    if ($content.Contains($needle)) { throw '部署ZIP文本匹配本机私有密钥；验证不通过，密钥值不会输出。' }
                }
            }
            finally { $reader.Dispose(); $stream.Dispose(); $content=$null }
        }
        $report = [ordered]@{
            passed = $true
            archive_files = $names.Count
            archive_bytes = (Get-Item -LiteralPath $zipPath).Length
            public_root_scripts_included = $true
            cloud_document_included = $true
            actual_local_secret_scan_passed = $true
            forbidden_paths_absent = $true
            includes_wheels = [bool]($names -match '^wheels/')
            sha256 = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
        }
        if ($ReportPath) { $report | ConvertTo-Json | Set-Content -LiteralPath $ReportPath -Encoding UTF8 }
        $report | ConvertTo-Json
    }
    finally { $archive.Dispose() }
}
finally { $needles.Clear() }
