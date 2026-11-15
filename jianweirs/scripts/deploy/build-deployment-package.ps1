[CmdletBinding()]
param(
    [string]$Destination,
    [switch]$IncludeWheels,
    [string]$PythonExecutable
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'worker-common.ps1')
$projectDir = Get-WorkerProjectDirectory
if (-not $Destination) {
    $Destination = Join-Path $projectDir ('outputs\deployment\omap-worker-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.zip')
}
$destinationPath = [System.IO.Path]::GetFullPath($Destination)
if ([System.IO.Path]::GetExtension($destinationPath) -ne '.zip') { throw 'Destination必须为.zip文件。' }
if (Test-Path -LiteralPath $destinationPath) { throw '目标ZIP已存在；请使用新文件名。不会覆盖现有部署包。' }
$stageRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath()).TrimEnd('\')
$stageName = 'omap-deployment-' + [guid]::NewGuid().ToString('N')
$stagePath = [System.IO.Path]::GetFullPath((Join-Path $stageRoot $stageName))
$stagedFiles = New-Object System.Collections.Generic.List[string]

function Add-PackageFile([string]$RelativePath) {
    $sourcePath = [System.IO.Path]::GetFullPath((Join-Path $projectDir $RelativePath))
    if (-not $sourcePath.StartsWith($projectDir + '\', [StringComparison]::OrdinalIgnoreCase)) { throw '源文件不在项目内。' }
    if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) { return }
    if ((Get-Item -LiteralPath $sourcePath).Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw '源代码打包不接受文件符号链接。'
    }
    $relative = $RelativePath.Replace('\', '/')
    if ($relative -match '(^|/)(\.venv|outputs|cache|__pycache__|\.git|\.pytest_cache|node_modules)(/|$)' -or
        $relative -match '(^|/)(\.env|\.worker\.env|cluster\.json|.*\.log|.*\.pid|.*\.pyc)$') {
        throw '敏感配置、运行数据或缓存不允许进入部署包。'
    }
    $targetPath = Join-Path $stagePath $RelativePath
    New-Item -ItemType Directory -Path ([IO.Path]::GetDirectoryName($targetPath)) -Force | Out-Null
    Copy-Item -LiteralPath $sourcePath -Destination $targetPath
    $stagedFiles.Add($relative)
}

try {
    New-Item -ItemType Directory -Path $stagePath | Out-Null
    # 白名单打包，不会递归复制根目录，因此隐藏配置/输出/大影像不会混入。
    foreach ($name in @('requirements.txt', 'start-api.ps1', 'stop-api.ps1',
                       'setup-gateway.ps1', 'start-gateway.ps1', 'stop-gateway.ps1',
                       'install-ngrok.ps1', 'setup-ngrok.ps1', 'start-ngrok.ps1', 'stop-ngrok.ps1',
                       'cluster.example.json', '.env.example', '.gitignore',
                       'README.md', 'FOUR_APIS.md', 'CLOUD_DEPLOYMENT.md')) {
        Add-PackageFile $name
    }
    foreach ($required in @('requirements.txt', 'start-api.ps1', 'stop-api.ps1')) {
        if (-not $stagedFiles.Contains($required)) { throw '部署源码不完整；缺少必要启动文件。' }
    }
    $sourceDir = Join-Path $projectDir 'omap_api'
    if (-not (Test-Path -LiteralPath $sourceDir -PathType Container)) { throw '缺少omap_api源码目录。' }
    if ((Get-Item -LiteralPath $sourceDir).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw '源码目录不能是符号链接。' }
    foreach ($file in Get-ChildItem -LiteralPath $sourceDir -File -Filter '*.py') {
        Add-PackageFile ('omap_api\' + $file.Name)
    }
    $resourceDir = Join-Path $sourceDir 'resources'
    if (Test-Path -LiteralPath $resourceDir -PathType Container) {
        foreach ($file in Get-ChildItem -LiteralPath $resourceDir -File -Filter '*.json') {
            Add-PackageFile ('omap_api\resources\' + $file.Name)
        }
    }
    foreach ($file in Get-ChildItem -LiteralPath $PSScriptRoot -File) {
        if ($file.Extension -in @('.ps1', '.md', '.py') -or $file.Name -in @('.worker.env.example', 'source-map-types.example.json')) {
            Add-PackageFile ('scripts\deploy\' + $file.Name)
        }
    }
    if ($IncludeWheels) {
        if ($PythonExecutable) {
            $pythonCommand = Get-Command -Name $PythonExecutable -CommandType Application -ErrorAction SilentlyContinue
            if ($null -eq $pythonCommand) { throw '找不到用于下载依赖的Python。' }
            $pythonPath = $pythonCommand.Source
        }
        else { $pythonPath = Join-Path $projectDir '.venv\Scripts\python.exe' }
        if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) { throw 'IncludeWheels需要现有Python；不会复制.venv。' }
        $wheelDir = Join-Path $stagePath 'wheels'
        New-Item -ItemType Directory -Path $wheelDir | Out-Null
        & $pythonPath '-m' 'pip' 'download' '--disable-pip-version-check' '--only-binary=:all:' `
            '--requirement' (Join-Path $stagePath 'requirements.txt') '--dest' $wheelDir
        if ($LASTEXITCODE -ne 0) { throw '依赖wheel下载失败；未生成不完整ZIP。' }
        foreach ($file in Get-ChildItem -LiteralPath $wheelDir -File) {
            if ($file.Extension -ne '.whl') { throw 'wheels目录包含非wheel文件。' }
            $stagedFiles.Add('wheels/' + $file.Name)
        }
        Write-Output '离线wheels与打包电脑的Python版本/Windows架构匹配，其他电脑应使用同版本64位Python。'
    }
    $manifest = [ordered]@{
        created_at = [DateTime]::UtcNow.ToString('o')
        package_type = 'Windows worker source deployment'
        includes_wheels = [bool]$IncludeWheels
        excludes = @('.env', '.worker.env', 'cluster.json', '.venv', 'outputs', 'cache', 'credentials', 'imagery')
        files = @($stagedFiles | Sort-Object)
    }
    $manifest | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $stagePath 'deployment-manifest.json') -Encoding UTF8
    New-Item -ItemType Directory -Path ([IO.Path]::GetDirectoryName($destinationPath)) -Force | Out-Null
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [System.IO.Compression.ZipFile]::CreateFromDirectory($stagePath, $destinationPath, [IO.Compression.CompressionLevel]::Optimal, $false)
    $archive = [System.IO.Compression.ZipFile]::OpenRead($destinationPath)
    try {
        foreach ($entry in $archive.Entries) {
            $entryName = $entry.FullName.Replace('\', '/')
            if ($entryName -match '(^|/)(\.env|\.worker\.env|cluster\.json|\.venv|outputs|cache|__pycache__|\.git|\.pytest_cache)(/|$)' -or
                $entryName -match '\.(png|jpg|jpeg|tif|tiff|zip|log|pid|pyc)$') {
                throw 'ZIP内容不符合排敏白名单；请检查部署包构建规则。'
            }
        }
    }
    finally { $archive.Dispose() }
    $hash = (Get-FileHash -LiteralPath $destinationPath -Algorithm SHA256).Hash.ToLowerInvariant()
    Write-Output "已生成不含账户配置/影像/虚拟环境的部署包：$destinationPath"
    Write-Output "SHA256：$hash"
}
finally {
    # 只删除本次明确创建的临时目录；递归删除前核验绝对路径及直接父目录。
    if (Test-Path -LiteralPath $stagePath -PathType Container) {
        $checked = [System.IO.Path]::GetFullPath($stagePath).TrimEnd('\')
        if (-not [string]::Equals([IO.Path]::GetDirectoryName($checked), $stageRoot, [StringComparison]::OrdinalIgnoreCase) -or
            [IO.Path]::GetFileName($checked) -ne $stageName -or
            (Get-Item -LiteralPath $checked).Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw '临时目录归属核验失败；没有递归删除任何目录。'
        }
        Remove-Item -LiteralPath $checked -Recurse -Force
    }
}
