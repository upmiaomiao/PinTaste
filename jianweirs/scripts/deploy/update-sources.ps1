[CmdletBinding()]
param([switch]$CheckOnly, [switch]$SkipScienceCrops)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'worker-common.ps1')
$projectDir = Get-WorkerProjectDirectory
$pythonPath = Get-WorkerPythonPath $projectDir
$statusDir = Join-Path $projectDir 'outputs\source-upgrade'
try {
    if (-not (Test-Path -LiteralPath (Join-Path $projectDir '.worker.env') -PathType Leaf) -or
        -not (Test-Path -LiteralPath (Join-Path $projectDir 'cluster.json') -PathType Leaf)) {
        throw '此更新必须在已部署目录运行。'
    }
    & $pythonPath (Join-Path $PSScriptRoot 'probe_runtime.py')
    if ($LASTEXITCODE -ne 0) { throw '运行环境检查失败。' }
    if ($CheckOnly) { Write-Output '更新环境检查通过；未停止服务、修改配置或发布隧道。'; return }
    Write-Host '正在更新本目录服务；现有Cloudflare隧道保持运行。请保持奥维登录。'
    Write-Host '将切换奥维地图进行取图与坐标核验，可能需要数分钟；不会显示PSK或网站密钥。'
    & (Join-Path $projectDir 'stop-gateway.ps1')
    & (Join-Path $PSScriptRoot 'stop-worker.ps1')
    & (Join-Path $PSScriptRoot 'start-worker.ps1')
    $null = New-Item -ItemType Directory -Path $statusDir -Force
    & $pythonPath (Join-Path $PSScriptRoot 'expand_sources.py') --worker-config (Join-Path $projectDir '.worker.env') --output $statusDir
    if ($LASTEXITCODE -ne 0) { throw '本地图源核验未完成；配置尚未替换。' }
    $report = Get-Content -LiteralPath (Join-Path $statusDir 'source-upgrade.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $mappingPath = Join-Path $statusDir 'source-map-types.json'
    & (Join-Path $PSScriptRoot 'setup-worker.ps1') -SkipDependencies -SourceMapTypesFile $mappingPath -OpticalCoordinateProfile $report.recommended_coordinate_profile
    & (Join-Path $PSScriptRoot 'stop-worker.ps1')
    & (Join-Path $PSScriptRoot 'start-worker.ps1')
    $privateTemp = Join-Path $projectDir ('.cluster-upgrade-' + [guid]::NewGuid().ToString('N') + '.tmp')
    try {
        [IO.File]::WriteAllText($privateTemp, '', [Text.UTF8Encoding]::new($false))
        Set-PrivateWorkerFileAcl $privateTemp
        & $pythonPath (Join-Path $PSScriptRoot 'deployment_admin.py') configure-gateway --root $projectDir --sources $mappingPath --output $privateTemp
        if ($LASTEXITCODE -ne 0) { throw '网关配置更新失败。' }
    }
    finally { if (Test-Path -LiteralPath $privateTemp -PathType Leaf) { Remove-Item -LiteralPath $privateTemp -Force } }
    & (Join-Path $projectDir 'start-gateway.ps1')
    & (Join-Path $projectDir 'start-quick-tunnel.ps1')
    $endpoint = Get-Content -LiteralPath (Join-Path $projectDir 'outputs\quick-tunnel-endpoint.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $publicUrl = [string]$endpoint.public_url
    & $pythonPath (Join-Path $PSScriptRoot 'wait_public_gateway.py') --base-url $publicUrl --timeout 45
    if ($LASTEXITCODE -ne 0) { throw '公网入口暂未就绪。' }
    $connectionPath = Join-Path $projectDir '网站后端接入.env'
    if (-not (Test-Path -LiteralPath $connectionPath -PathType Leaf)) {
        [IO.File]::WriteAllText($connectionPath, '', [Text.UTF8Encoding]::new($false))
    }
    Set-PrivateWorkerFileAcl $connectionPath
    & $pythonPath (Join-Path $PSScriptRoot 'deployment_admin.py') export-connection --root $projectDir --public-url $publicUrl --output $connectionPath
    if ($LASTEXITCODE -ne 0) { throw '后端连接配置导出失败。' }
    $sources = $report.gateway_optical_sources -join ','
    $verifyArgs = @('--base-url', $publicUrl, '--cluster-config', (Join-Path $projectDir 'cluster.json'),
        '--optical-sources', $sources, '--output', (Join-Path $statusDir 'public-verification.json'))
    if ($SkipScienceCrops) { $verifyArgs += '--no-run-science-crops' }
    & $pythonPath (Join-Path $PSScriptRoot 'verify_public_api.py') @verifyArgs
    $deliveryExit = $LASTEXITCODE
    & $pythonPath (Join-Path $PSScriptRoot 'verify_source_alignment.py') --base-url $publicUrl --cluster-config (Join-Path $projectDir 'cluster.json') --sources-file $mappingPath --output (Join-Path $statusDir 'public-alignment.json')
    $alignmentExit = $LASTEXITCODE
    $lines = @(("公网入口：" + $publicUrl), ("更新时间：" + [DateTime]::Now.ToString('yyyy-MM-dd HH:mm:ss')),
        ("已取图的逻辑图源：" + $sources), ("本机坐标模式：" + $report.recommended_coordinate_profile),
        '取图、权限和配准结论分别记录；未通过配准的影像只能作背景参考。',
        '报告：outputs/source-upgrade/source-upgrade.json、public-verification.json、public-alignment.json。',
        '网站后端接入.env 含私有Bearer密钥，只交给网站后端；不要放到网页。',
        'Cloudflare临时入口重建后地址会变化。')
    [IO.File]::WriteAllLines((Join-Path $projectDir '公网地址.txt'), $lines, [Text.UTF8Encoding]::new($true))
    Write-Host ('公网入口：' + $publicUrl)
    Write-Host ('图源更新完成；交付验收退出码：' + $deliveryExit + '；配准验收退出码：' + $alignmentExit)
    Write-Host '原网站密钥保持不变。未取到图的来源没有启用；配准不足会标记为未知。'
}
catch {
    # Recover service availability with the last saved configuration. Neither
    # recovery nor the update invokes ngrok or stops the Cloudflare process.
    if (-not $CheckOnly) {
        try { & (Join-Path $PSScriptRoot 'start-worker.ps1') } catch { }
        try { & (Join-Path $projectDir 'start-gateway.ps1') } catch { }
    }
    Write-Host '更新未完成；请保留此目录并提供outputs/source-upgrade内的报告，不要提供私有env或密钥。' -ForegroundColor Red
    exit 1
}
