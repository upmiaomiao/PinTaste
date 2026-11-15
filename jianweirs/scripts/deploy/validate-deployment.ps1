[CmdletBinding()]
param([string]$PythonExecutable)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$projectDir = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..')).TrimEnd('\')
if (-not $PythonExecutable) { $PythonExecutable = (Get-Command python.exe -CommandType Application).Source }
$tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\')
$fixtureName = 'omap-deploy-validation-' + [guid]::NewGuid().ToString('N')
$fixtureRoot = [IO.Path]::GetFullPath((Join-Path $tempRoot $fixtureName))
$checks = New-Object System.Collections.Generic.List[string]

function Assert-Deployment([bool]$Condition, [string]$Name) {
    if (-not $Condition) { throw ('FAIL: ' + $Name) }
    $checks.Add($Name)
    Write-Output ('PASS: ' + $Name)
}

try {
    New-Item -ItemType Directory -Path (Join-Path $fixtureRoot 'scripts\deploy') -Force | Out-Null
    foreach ($script in Get-ChildItem -LiteralPath $PSScriptRoot -File) {
        if ($script.Extension -in @('.ps1', '.md') -or $script.Name -in @('.worker.env.example', 'source-map-types.example.json')) {
            Copy-Item -LiteralPath $script.FullName -Destination (Join-Path $fixtureRoot ('scripts\deploy\' + $script.Name))
        }
    }
    Copy-Item -LiteralPath (Join-Path $projectDir 'requirements.txt') -Destination (Join-Path $fixtureRoot 'requirements.txt')
    New-Item -ItemType Directory -Path (Join-Path $fixtureRoot 'omap_api') -Force | Out-Null
    [IO.File]::WriteAllText((Join-Path $fixtureRoot 'omap_api\app.py'), '# Deployment fixture, no real service started.', [Text.UTF8Encoding]::new($false))
    [IO.File]::WriteAllText((Join-Path $fixtureRoot 'README.md'), '# Offline deployment validation fixture.', [Text.UTF8Encoding]::new($false))
    # These launcher stubs verify wrapper arguments/environment without stopping
    # or restarting the real user API or starting a network listener.
    $stubStart = @'
param([int]$Port=8765,[string]$ListenHost='127.0.0.1')
if ([string]::IsNullOrWhiteSpace($env:OMAP_WORKER_API_KEY)) { throw 'Missing worker bearer key' }
$folder=Join-Path $PSScriptRoot 'outputs'
New-Item -ItemType Directory -Path $folder -Force | Out-Null
$record=@{pid=$PID;start_time_utc_ticks=(Get-Process -Id $PID).StartTime.ToUniversalTime().Ticks.ToString();host=$ListenHost;port=$Port}
Set-Content -LiteralPath (Join-Path $folder 'api.pid') -Value $PID
$record | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $folder 'api-process.json') -Encoding UTF8
'@
    $stubStop = @'
$folder=Join-Path $PSScriptRoot 'outputs'
foreach($name in @('api.pid','api-process.json')){
    $path=Join-Path $folder $name
    if(Test-Path -LiteralPath $path){Remove-Item -LiteralPath $path}
}
'@
    [IO.File]::WriteAllText((Join-Path $fixtureRoot 'start-api.ps1'), $stubStart, [Text.UTF8Encoding]::new($false))
    [IO.File]::WriteAllText((Join-Path $fixtureRoot 'stop-api.ps1'), $stubStop, [Text.UTF8Encoding]::new($false))
    foreach ($path in @(Get-ChildItem -LiteralPath $PSScriptRoot -Filter '*.ps1' -File | ForEach-Object FullName) + @((Join-Path $projectDir 'start-api.ps1'))) {
        $tokens=$null; $parseErrors=$null
        [Management.Automation.Language.Parser]::ParseFile($path,[ref]$tokens,[ref]$parseErrors) | Out-Null
        Assert-Deployment ($parseErrors.Count -eq 0) ('PowerShell syntax: ' + [IO.Path]::GetFileName($path))
    }
    $setup = Join-Path $fixtureRoot 'scripts\deploy\setup-worker.ps1'
    $start = Join-Path $fixtureRoot 'scripts\deploy\start-worker.ps1'
    $stop = Join-Path $fixtureRoot 'scripts\deploy\stop-worker.ps1'
    $builder = Join-Path $fixtureRoot 'scripts\deploy\build-deployment-package.ps1'
    & $setup -PythonExecutable $PythonExecutable -SkipDependencies
    . (Join-Path $fixtureRoot 'scripts\deploy\worker-common.ps1')
    $privateConfig = Join-Path $fixtureRoot '.worker.env'
    $firstConfig = Get-WorkerConfiguration $privateConfig
    $firstKey = $firstConfig['OMAP_WORKER_API_KEY']
    Assert-Deployment ($firstKey -match '^[A-Za-z0-9_-]{43}$') 'Random 256-bit worker key created without printing it'
    Assert-Deployment (Test-Path -LiteralPath (Join-Path $fixtureRoot '.venv\Scripts\python.exe') -PathType Leaf) 'Local venv created; no foreign venv copied'
    $acl = Get-Acl -LiteralPath $privateConfig
    Assert-Deployment $acl.AreAccessRulesProtected 'Private env ACL inheritance disabled'
    $secondOutput = & $setup -SkipDependencies | Out-String
    $secondConfig = Get-WorkerConfiguration $privateConfig
    Assert-Deployment ($secondConfig['OMAP_WORKER_API_KEY'] -eq $firstKey) 'Repeated setup preserves worker key'
    Assert-Deployment (-not $secondOutput.Contains($firstKey)) 'Setup output does not reveal service key'
    $before = [Environment]::GetEnvironmentVariable('OMAP_WORKER_API_KEY','Process')
    $startOutput = & $start | Out-String
    Assert-Deployment (-not $startOutput.Contains($firstKey)) 'Start wrapper does not reveal service key'
    Assert-Deployment ([Environment]::GetEnvironmentVariable('OMAP_WORKER_API_KEY','Process') -eq $before) 'Calling shell environment restored after launch'
    $null = & $start
    Assert-Deployment (Test-Path -LiteralPath (Join-Path $fixtureRoot 'outputs\worker-process.json')) 'Verified same worker can be started idempotently'
    $blocked=$false
    try { $null = & $start -ListenHost '0.0.0.0' } catch { $blocked=$true }
    Assert-Deployment $blocked 'Implicit all-interface binding refused'
    $null = & $stop
    Assert-Deployment (-not (Test-Path -LiteralPath (Join-Path $fixtureRoot 'outputs\worker-process.json'))) 'Stop wrapper cleans only its runtime record'
    try {
        [Environment]::SetEnvironmentVariable('OMAP_WORKER_API_KEY', $firstKey, 'Process')
        & (Join-Path $fixtureRoot 'start-api.ps1')
    }
    finally {
        if ($null -eq $before) { Remove-Item -LiteralPath 'Env:\OMAP_WORKER_API_KEY' -ErrorAction SilentlyContinue }
        else { [Environment]::SetEnvironmentVariable('OMAP_WORKER_API_KEY', $before, 'Process') }
    }
    # A launcher lacking worker metadata must not be reclassified as authenticated.
    $blocked=$false
    try { $null = & $start } catch { $blocked=$true }
    Assert-Deployment $blocked 'Unverified existing API is not silently reused as worker'
    $null = & $stop
    $fixtureSecret = 'DEPLOYMENT-VALIDATION-PRIVATE-MARKER-' + [guid]::NewGuid().ToString('N')
    [IO.File]::WriteAllText((Join-Path $fixtureRoot '.env'), "OMAP_PSK=$fixtureSecret", [Text.UTF8Encoding]::new($false))
    [IO.File]::WriteAllText((Join-Path $fixtureRoot 'cluster.json'), $fixtureSecret, [Text.UTF8Encoding]::new($false))
    foreach ($folder in @('outputs', 'cache', '.venv', 'omap_api\__pycache__')) {
        $path = Join-Path $fixtureRoot $folder
        New-Item -ItemType Directory -Path $path -Force | Out-Null
        [IO.File]::WriteAllText((Join-Path $path 'private-data.txt'), $fixtureSecret, [Text.UTF8Encoding]::new($false))
    }
    $zipPath = Join-Path $fixtureRoot 'validation.zip'
    $null = & $builder -Destination $zipPath
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [IO.Compression.ZipFile]::OpenRead($zipPath)
    try {
        $names = @($archive.Entries | ForEach-Object { $_.FullName.Replace('\', '/') })
        Assert-Deployment ($names -contains 'requirements.txt' -and $names -contains 'omap_api/app.py') 'Archive includes source and dependency manifest'
        Assert-Deployment (-not ($names -match '(^|/)(\.env|\.worker\.env|cluster\.json|\.venv|outputs|cache|__pycache__)(/|$)')) 'Archive excludes private files, venv, output and cache directories'
        $safe=$true
        foreach ($entry in $archive.Entries) {
            $stream = $entry.Open()
            $reader = [IO.StreamReader]::new($stream,[Text.Encoding]::UTF8)
            try {
                $content=$reader.ReadToEnd()
                if ($content.Contains($firstKey) -or $content.Contains($fixtureSecret)) { $safe=$false }
            }
            finally { $reader.Dispose(); $stream.Dispose() }
        }
        Assert-Deployment $safe 'Archive text contains neither generated key nor seeded private markers'
    }
    finally { $archive.Dispose() }
    $blocked=$false
    try { $null = & $builder -Destination $zipPath } catch { $blocked=$true }
    Assert-Deployment $blocked 'Archive builder refuses overwriting an existing ZIP'
    Write-Output ("Offline deployment validation: $($checks.Count)/$($checks.Count) passed. Real API was not restarted; dependency install/network launch excluded.")
}
finally {
    if (Test-Path -LiteralPath $fixtureRoot -PathType Container) {
        $checked=[IO.Path]::GetFullPath($fixtureRoot).TrimEnd('\')
        if ([IO.Path]::GetDirectoryName($checked) -ne $tempRoot -or [IO.Path]::GetFileName($checked) -ne $fixtureName -or
            (Get-Item -LiteralPath $checked).Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw 'Fixture path ownership check failed; recursive delete skipped.'
        }
        Remove-Item -LiteralPath $checked -Recurse -Force
    }
}
