[CmdletBinding()]
param(
    [ValidateRange(1024, 65535)][int]$Port = 8765,
    [string]$ListenHost = '127.0.0.1'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'worker-common.ps1')
$projectDir = Get-WorkerProjectDirectory
$config = Get-WorkerConfiguration (Join-Path $projectDir '.worker.env')
$address = $null
if (-not [System.Net.IPAddress]::TryParse($ListenHost, [ref]$address) -or
    $address.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork -or
    $ListenHost -eq '0.0.0.0') {
    throw 'ListenHost仅支持127.0.0.1或明确指定的本机IPv4；不能默认开放全部网卡。'
}
$pidPath = Join-Path $projectDir 'outputs\api.pid'
$apiRecordPath = Join-Path $projectDir 'outputs\api-process.json'
$workerRecordPath = Join-Path $projectDir 'outputs\worker-process.json'
$fingerprint = Get-WorkerKeyFingerprint $config['OMAP_WORKER_API_KEY']
$configurationFingerprint = Get-WorkerConfigurationFingerprint $config
$running = $false
if (Test-Path -LiteralPath $pidPath -PathType Leaf) {
    $apiPid = 0
    if (-not [int]::TryParse((Get-Content -LiteralPath $pidPath -Raw).Trim(), [ref]$apiPid)) { throw 'API PID记录不正确；未启动新worker。' }
    $running = $null -ne (Get-Process -Id $apiPid -ErrorAction SilentlyContinue)
    if ($running) {
        if (-not (Test-Path -LiteralPath $workerRecordPath -PathType Leaf)) {
            throw '当前API不是已核验的鉴权worker。设置新密钥不会改变已启动进程；请先运行stop-api.ps1，再启动worker。'
        }
        if (-not (Test-Path -LiteralPath $apiRecordPath -PathType Leaf)) { throw '缺少API进程归属记录；未启动新worker。' }
        $existing = Get-Content -LiteralPath $workerRecordPath -Raw | ConvertFrom-Json
        $apiRecord = Get-Content -LiteralPath $apiRecordPath -Raw | ConvertFrom-Json
        if ([int]$existing.pid -ne $apiPid -or $existing.start_time_utc_ticks -ne $apiRecord.start_time_utc_ticks -or
            $existing.key_fingerprint -ne $fingerprint -or $existing.configuration_fingerprint -ne $configurationFingerprint -or $existing.listen_host -ne $ListenHost -or
            [int]$existing.port -ne $Port) {
            throw '运行中worker的进程或配置与本次请求不同；请先停止本项目API，再使用新配置启动。'
        }
    }
}
$previous = @{}
try {
    foreach ($name in $config.Keys) {
        # 空PSK让应用在本机已有.env中读取；绝不从其他电脑复制凭证。
        if (-not [string]::IsNullOrEmpty([string]$config[$name])) {
            $previous[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
            [Environment]::SetEnvironmentVariable($name, [string]$config[$name], 'Process')
        }
    }
    & (Join-Path $projectDir 'start-api.ps1') -Port $Port -ListenHost $ListenHost
    $record = Get-Content -LiteralPath $apiRecordPath -Raw | ConvertFrom-Json
    $workerRecord = [ordered]@{
        pid = [int]$record.pid
        start_time_utc_ticks = [string]$record.start_time_utc_ticks
        listen_host = $ListenHost
        port = $Port
        key_fingerprint = $fingerprint
        configuration_fingerprint = $configurationFingerprint
        authentication = 'bearer'
    }
    $workerRecord | ConvertTo-Json | Set-Content -LiteralPath $workerRecordPath -Encoding UTF8
    Set-PrivateWorkerFileAcl $workerRecordPath
}
finally {
    foreach ($name in $previous.Keys) {
        if ($null -eq $previous[$name]) { Remove-Item -LiteralPath ('Env:\' + $name) -ErrorAction SilentlyContinue }
        else { [Environment]::SetEnvironmentVariable($name, $previous[$name], 'Process') }
    }
    $config = $null
    $fingerprint = $null
    $configurationFingerprint = $null
}
Write-Output '鉴权worker启动配置已应用；服务密钥仅存本机.worker.env，未放入命令行或日志。'
Write-Output '公网访问请走gateway；worker通道同样必须携带Bearer服务密钥。向日葵仅用于维护，不作为数据通道。'
