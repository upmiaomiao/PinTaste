[CmdletBinding()]
param()
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'worker-common.ps1')
$projectDir = Get-WorkerProjectDirectory
& (Join-Path $projectDir 'stop-api.ps1')
$workerRecordPath = Join-Path $projectDir 'outputs\worker-process.json'
if (Test-Path -LiteralPath $workerRecordPath -PathType Leaf) {
    Remove-Item -LiteralPath $workerRecordPath -Force
}
Write-Output 'worker已停止；奥维、私有配置、影像和日志均保留。'
