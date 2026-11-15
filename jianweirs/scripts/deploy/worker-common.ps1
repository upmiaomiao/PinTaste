Set-StrictMode -Version Latest

function Get-WorkerProjectDirectory {
    return [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..')).TrimEnd('\')
}

function Get-WorkerPythonPath([string]$ProjectDirectory) {
    $portable = Join-Path $ProjectDirectory 'runtime\python.exe'
    if (Test-Path -LiteralPath $portable -PathType Leaf) { return $portable }
    return Join-Path $ProjectDirectory '.venv\Scripts\python.exe'
}

function Set-PrivateWorkerFileAcl([string]$Path) {
    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    # 从文件现有ACL仅修改DACL，避免创建带审计/owner段的新描述符要求SeSecurityPrivilege。
    $security = Get-Acl -LiteralPath $Path
    $security.SetAccessRuleProtection($true, $false)
    foreach ($rule in @($security.Access)) { $null = $security.RemoveAccessRuleSpecific($rule) }
    $security.SetAccessRule([System.Security.AccessControl.FileSystemAccessRule]::new(
        $identity.User, [System.Security.AccessControl.FileSystemRights]::FullControl,
        [System.Security.AccessControl.AccessControlType]::Allow))
    $systemSid = [System.Security.Principal.SecurityIdentifier]::new('S-1-5-18')
    $security.SetAccessRule([System.Security.AccessControl.FileSystemAccessRule]::new(
        $systemSid, [System.Security.AccessControl.FileSystemRights]::FullControl,
        [System.Security.AccessControl.AccessControlType]::Allow))
    $accessOnly = New-Object System.Security.AccessControl.FileSecurity
    $accessSection = [System.Security.AccessControl.AccessControlSections]::Access
    $accessOnly.SetSecurityDescriptorSddlForm($security.GetSecurityDescriptorSddlForm($accessSection), $accessSection)
    $file = Get-Item -LiteralPath $Path
    if ($PSVersionTable.PSVersion.Major -ge 6) {
        [System.IO.FileSystemAclExtensions]::SetAccessControl($file, $accessOnly)
    }
    else { $file.SetAccessControl($accessOnly) }
}

function Get-WorkerConfiguration([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw '缺少本机 .worker.env；请先运行 scripts\deploy\setup-worker.ps1。'
    }
    if ((Get-Item -LiteralPath $Path).Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
        throw '私有 worker 配置不能是符号链接。'
    }
    $allowed = @('OMAP_WORKER_API_KEY', 'OMAP_WS_URL', 'OMAP_PSK',
                 'OMAP_DEFAULT_MAP_TYPE', 'OMAP_SOURCE_MAP_TYPES_JSON', 'OMAP_OUTPUT_DIR',
                 'OMAP_OPTICAL_COORDINATE_PROFILE')
    $config = @{}
    foreach ($line in Get-Content -LiteralPath $Path -Encoding UTF8) {
        if ([string]::IsNullOrWhiteSpace($line) -or $line.TrimStart().StartsWith('#')) { continue }
        $separator = $line.IndexOf('=')
        if ($separator -le 0) { throw 'worker 配置存在格式错误；每行必须为 NAME=value。' }
        $name = $line.Substring(0, $separator).Trim()
        if ($allowed -notcontains $name -or $config.ContainsKey($name)) {
            throw 'worker 配置存在未知或重复字段；未导入任何配置。'
        }
        $config[$name] = $line.Substring($separator + 1)
    }
    if (-not $config.ContainsKey('OMAP_WORKER_API_KEY') -or
        $config['OMAP_WORKER_API_KEY'] -notmatch '^[A-Za-z0-9_-]{43,128}$') {
        throw '缺少有效 worker 服务密钥；请运行 setup-worker 生成私有配置。'
    }
    if ($config.ContainsKey('OMAP_WS_URL') -and $config['OMAP_WS_URL']) {
        Test-WorkerOmapUrl $config['OMAP_WS_URL']
    }
    if ($config.ContainsKey('OMAP_SOURCE_MAP_TYPES_JSON') -and $config['OMAP_SOURCE_MAP_TYPES_JSON']) {
        $null = ConvertTo-WorkerSourceMapJson $config['OMAP_SOURCE_MAP_TYPES_JSON']
    }
    if ($config.ContainsKey('OMAP_OPTICAL_COORDINATE_PROFILE') -and
        $config['OMAP_OPTICAL_COORDINATE_PROFILE'] -notin @('documented', 'wgs84_code1')) {
        throw '光学坐标profile必须为documented或wgs84_code1；变更后需重新核验影像控制点。'
    }
    return $config
}

function Test-WorkerOmapUrl([string]$Value) {
    $parsed = $null
    if (-not [System.Uri]::TryCreate($Value, [System.UriKind]::Absolute, [ref]$parsed) -or
        $parsed.Scheme -notin @('ws', 'wss') -or
        $parsed.Host -notin @('127.0.0.1', 'localhost', '::1') -or
        $parsed.UserInfo -or $parsed.Query -or $parsed.Fragment -or
        $parsed.Port -lt 1 -or $parsed.AbsolutePath -ne '/') {
        throw 'OMAP_WS_URL 必须是本机无用户名、无查询参数的 ws/wss 地址。'
    }
}

function ConvertTo-WorkerSourceMapJson([string]$Value) {
    try { $parsed = $Value | ConvertFrom-Json }
    catch { throw '图源 ID 配置必须是 JSON 对象，不包含提供商 URL 或密钥。' }
    if ($parsed -isnot [System.Management.Automation.PSCustomObject]) {
        throw '图源 ID 配置必须是 source 名称到整数 MapType 的 JSON 对象。'
    }
    $validSources = @('bing', 'amap', 'tencent', 'google', 'mapbox', 'mapquest',
                      'tianditu', 'siwei', 'siwei_daily')
    $result = [ordered]@{}
    foreach ($property in $parsed.PSObject.Properties) {
        if ($validSources -notcontains $property.Name -or
            $property.Value -isnot [ValueType] -or
            $property.Value -is [bool] -or
            $property.Value -is [double] -or $property.Value -is [decimal] -or
            [long]$property.Value -lt 1 -or [long]$property.Value -gt 2147483647) {
            throw '图源 ID 配置含有不支持的 source 或非正整数 MapType。'
        }
        $result[$property.Name] = [int]$property.Value
    }
    return ConvertTo-Json -InputObject $result -Compress
}

function Get-WorkerKeyFingerprint([string]$Value) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($Value)
        return ([System.BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant()
    }
    finally { $sha.Dispose() }
}

function Get-WorkerConfigurationFingerprint([hashtable]$Config) {
    $normalized = @()
    foreach ($name in ($Config.Keys | Sort-Object)) { $normalized += "$name=$($Config[$name])" }
    return Get-WorkerKeyFingerprint ($normalized -join "`n")
}
