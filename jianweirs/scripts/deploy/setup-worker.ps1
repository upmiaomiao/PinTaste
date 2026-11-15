[CmdletBinding()]
param(
    [string]$PythonExecutable,
    [string]$OmapWsUrl,
    [System.Security.SecureString]$OmapPsk,
    [switch]$PromptForOmapPsk,
    [string]$SourceMapTypesFile,
    [ValidateSet('documented', 'wgs84_code1')][string]$OpticalCoordinateProfile,
    [switch]$SkipDependencies,
    [switch]$OfflineWheels
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'worker-common.ps1')

$projectDir = Get-WorkerProjectDirectory
$venvDir = Join-Path $projectDir '.venv'
$venvPython = Get-WorkerPythonPath $projectDir
$portableRuntime = Test-Path -LiteralPath (Join-Path $projectDir 'runtime\python.exe') -PathType Leaf
$requirementsPath = Join-Path $projectDir 'requirements.txt'
$configPath = Join-Path $projectDir '.worker.env'
if (-not (Test-Path -LiteralPath $requirementsPath -PathType Leaf)) {
    throw '未找到项目 requirements.txt；请先完整解压部署包。'
}

if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    $pythonPrefix = @()
    if ($PythonExecutable) {
        $command = Get-Command -Name $PythonExecutable -CommandType Application -ErrorAction SilentlyContinue
        if ($null -eq $command) { throw '找不到指定 Python；请安装64位Python后提供完整 python.exe 路径。' }
    }
    else {
        $command = Get-Command -Name 'py.exe' -CommandType Application -ErrorAction SilentlyContinue
        if ($null -ne $command) { $pythonPrefix = @('-3') }
        else { $command = Get-Command -Name 'python.exe' -CommandType Application -ErrorAction SilentlyContinue }
    }
    if ($null -eq $command) { throw '需要64位Python 3.11或更新版本；本项目实测Python 3.13。无需复制其他电脑的.venv。' }
    $probeCode = 'import struct,sys; assert sys.version_info >= (3,11) and struct.calcsize(chr(80))==8; print(sys.version.split()[0])'
    $pythonVersion = & $command.Source @pythonPrefix '-c' $probeCode
    if ($LASTEXITCODE -ne 0) { throw 'Python版本/位数不满足要求；请选择64位Python 3.11或更新版本。' }
    Write-Output "正在创建本机独立 .venv，Python $pythonVersion。"
    & $command.Source @pythonPrefix '-m' 'venv' $venvDir
    if ($LASTEXITCODE -ne 0) { throw '创建.venv失败；未生成worker密钥。' }
}
elseif (-not $portableRuntime -and ((Get-Item -LiteralPath $venvDir).Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
    throw '本机.venv不能是其他目录的符号链接。'
}

if ($portableRuntime) {
    & $venvPython (Join-Path $PSScriptRoot 'probe_runtime.py')
    if ($LASTEXITCODE -ne 0) { throw '便携运行环境不完整；请重新解压完整部署包。' }
}
elseif (-not $SkipDependencies) {
    $pipArgs = @('-m', 'pip', 'install', '--disable-pip-version-check', '--requirement', $requirementsPath)
    if ($OfflineWheels) {
        $wheelDir = Join-Path $projectDir 'wheels'
        if (-not (Test-Path -LiteralPath $wheelDir -PathType Container)) { throw '部署包不含wheels；请联网安装，或先构建包含wheels的包。' }
        $pipArgs += @('--no-index', '--find-links', $wheelDir)
    }
    & $venvPython @pipArgs
    if ($LASTEXITCODE -ne 0) { throw '依赖安装失败；修复网络/Python后可重复运行setup-worker。' }
}

if (Test-Path -LiteralPath $configPath -PathType Leaf) {
    $config = Get-WorkerConfiguration $configPath
    Write-Output '已保留本机现有worker服务密钥。'
}
else {
    $random = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $randomBytes = New-Object byte[] 32
        $random.GetBytes($randomBytes)
        $serviceKey = [Convert]::ToBase64String($randomBytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
    }
    finally { $random.Dispose() }
    $config = @{
        OMAP_WORKER_API_KEY = $serviceKey
        OMAP_WS_URL = 'ws://127.0.0.1:7699'
        OMAP_PSK = ''
        OMAP_DEFAULT_MAP_TYPE = '337'
        OMAP_SOURCE_MAP_TYPES_JSON = '{}'
        OMAP_OUTPUT_DIR = 'outputs/images'
    }
}
if ($OmapWsUrl) { Test-WorkerOmapUrl $OmapWsUrl; $config['OMAP_WS_URL'] = $OmapWsUrl }
if ($SourceMapTypesFile) {
    $config['OMAP_SOURCE_MAP_TYPES_JSON'] = ConvertTo-WorkerSourceMapJson (Get-Content -LiteralPath $SourceMapTypesFile -Raw -Encoding UTF8)
}
if ($OpticalCoordinateProfile) { $config['OMAP_OPTICAL_COORDINATE_PROFILE'] = $OpticalCoordinateProfile }
if ($PromptForOmapPsk) { $OmapPsk = Read-Host -Prompt '输入本机奥维第三方接口PSK（不显示）' -AsSecureString }
if ($null -ne $OmapPsk) {
    $pointer = [IntPtr]::Zero
    try {
        $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($OmapPsk)
        $plainPsk = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
        if ([string]::IsNullOrWhiteSpace($plainPsk) -or $plainPsk -match '[\r\n]') { throw '奥维PSK不能为空或包含换行；未显示PSK。' }
        $config['OMAP_PSK'] = $plainPsk
    }
    finally {
        if ($pointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
        $plainPsk = $null
    }
}
$header = @('# 仅本机使用；不要上传、复制到部署包或发给其他人。',
            '# NAME=value 不使用引号；空OMAP_PSK允许应用读取本机已有.env，否则仅科学接口可用。')
$lines = @($header)
foreach ($name in @('OMAP_WORKER_API_KEY', 'OMAP_WS_URL', 'OMAP_PSK', 'OMAP_DEFAULT_MAP_TYPE', 'OMAP_SOURCE_MAP_TYPES_JSON', 'OMAP_OUTPUT_DIR', 'OMAP_OPTICAL_COORDINATE_PROFILE')) {
    if ($config.ContainsKey($name)) {
        $value = [string]$config[$name]
        if ($value -match '[\r\n]') { throw '配置值不能包含换行；未写入私有配置。' }
        $lines += "$name=$value"
    }
}
$temporaryPath = Join-Path $projectDir ('.worker-env-' + [guid]::NewGuid().ToString('N') + '.tmp')
try {
    # 先创建空文件并收紧ACL，之后才写入私有内容。
    [System.IO.File]::WriteAllText($temporaryPath, '', [System.Text.UTF8Encoding]::new($false))
    Set-PrivateWorkerFileAcl $temporaryPath
    [System.IO.File]::WriteAllLines($temporaryPath, $lines, [System.Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $temporaryPath -Destination $configPath -Force
    Set-PrivateWorkerFileAcl $configPath
}
finally {
    if (Test-Path -LiteralPath $temporaryPath -PathType Leaf) { Remove-Item -LiteralPath $temporaryPath -Force }
    $serviceKey = $null
    $config = $null
    $lines = $null
}
Write-Output '本机worker已准备，私有配置为项目根目录.worker.env，服务密钥不会显示。'
Write-Output '奥维取图需要本机奥维已登录、第三方WebSocket已启用；9.8.5的PSK建议不超过16字符。'
Write-Output '每台电脑自行核对本机MapType，可用SourceMapTypesFile覆盖，不能假定自定义图源ID跨电脑相同。'
Write-Output '若尚未配置本机PSK，可再次运行setup-worker.ps1 -PromptForOmapPsk；随后运行start-worker.ps1。'
