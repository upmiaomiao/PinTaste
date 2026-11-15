[CmdletBinding()]
param([switch]$FailureReasonsOnly)
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
if ($PSVersionTable.PSVersion.Major -ne 5) {
    $validationArgs=@('-NoProfile','-ExecutionPolicy','Bypass','-File',$PSCommandPath)
    if($FailureReasonsOnly){$validationArgs+='-FailureReasonsOnly'}
    & "$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe" @validationArgs
    if ($LASTEXITCODE -ne 0) { throw 'PowerShell5 ngrok mock validation failed.' }
    return
}
$root=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$tempRoot=[IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\')
$name='ngrok-start-validation-'+[guid]::NewGuid().ToString('N')
$fixture=[IO.Path]::GetFullPath((Join-Path $tempRoot $name))
$checks=New-Object System.Collections.Generic.List[string]
$testProcesses=New-Object System.Collections.Generic.List[object]
$global:NgrokValidationHealthOk=$true
$global:NgrokValidationAuthStatus=401

function Assert-Test([bool]$Condition,[string]$Label) {
    if(-not $Condition){throw ('FAIL '+$Label)}
    $checks.Add($Label)
    Write-Output ('PASS '+$Label)
}
if($FailureReasonsOnly){
    # Extract only the production classifier. Synthetic log content stays in memory;
    # this mode creates no fixture, starts no process, and reads no account config.
    $tokens=$null;$parseErrors=$null
    $ast=[Management.Automation.Language.Parser]::ParseFile((Join-Path $root 'start-ngrok.ps1'),[ref]$tokens,[ref]$parseErrors)
    Assert-Test ($parseErrors.Count -eq 0) 'PowerShell5 production script syntax'
    $failureFunction=$ast.Find({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Get-NgrokSafeFailure'},$true)
    Assert-Test ($null -ne $failureFunction) 'Production safe-failure classifier found'
    try{
        & {
            function Test-Path {param([string]$LiteralPath,[string]$PathType) return $true}
            function Get-Content {param([string]$LiteralPath,[int]$Tail,[string]$Encoding,[string]$ErrorAction) return $fakeLogText}
            . ([scriptblock]::Create($failureFunction.Extent.Text))
            $cases=@(
                @{code='334';text='ERR_NGROK_334 endpoint https://fake.ngrok.example is already online FAKE_PRIVATE_TOKEN_MARKER';pattern='ERR_NGROK_334.*域名已被在线隧道占用'},
                @{code='4018';text='ERR_NGROK_4018 authtoken required FAKE_PRIVATE_TOKEN_MARKER';pattern='ngrok账户尚未认证'},
                @{code='108';text='ERR_NGROK_108 simultaneous agent session limit FAKE_PRIVATE_TOKEN_MARKER';pattern='agent数量达到限制'},
                @{code='105';text='ERR_NGROK_105 FAKE_PRIVATE_TOKEN_MARKER';pattern='ngrok拒绝当前账户认证'},
                @{code='unknown';text='ERR_NGROK_9999 FAKE_PRIVATE_TOKEN_MARKER';pattern='ERR_NGROK_9999'},
                @{code='timeout';text='FAKE_PRIVATE_TOKEN_MARKER';pattern='未在启动期限内确认目标HTTPS入口'}
            )
            foreach($case in $cases){
                $fakeLogText=$case.text
                $message=Get-NgrokSafeFailure @('synthetic-log-only')
                Assert-Test ($message -match $case.pattern) ('Safe failure '+$case.code+' classified correctly')
                Assert-Test (-not $message.Contains('FAKE_PRIVATE_TOKEN_MARKER')) ('Safe failure '+$case.code+' never reflects log content')
                if($case.code -eq '334'){
                    Assert-Test ($message.Contains('独立URL') -and $message.Contains('确认可停的旧隧道')) '334 offers independent URL or confirmed safe stop'
                    Assert-Test ($message.Contains('不要开启pooling连接不同业务')) '334 discourages pooling different services'
                    Assert-Test ($message -notmatch 'authtoken|额度|达到限制') '334 never mislabels token or quota failure'
                    Assert-Test ($message -match '^ngrok未建立入口') '334 remains compatible with deployment safe-reason filter'
                }
            }
        }
        Write-Output ("Ngrok safe-failure classifier checks: $($checks.Count)/$($checks.Count) PASS; no process, real log, or token used.")
    }
    finally{Remove-Variable -Name NgrokValidationHealthOk,NgrokValidationAuthStatus -Scope Global -ErrorAction SilentlyContinue}
    return
}
function Free-Port {
    $listener=[Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback,0)
    try{$listener.Start();return ([Net.IPEndPoint]$listener.LocalEndpoint).Port}finally{$listener.Stop()}
}
function Invoke-RestMethod {
    param([string]$Uri,[int]$TimeoutSec)
    if($Uri -match '/health$'){if($global:NgrokValidationHealthOk){return @{status='ok'}}else{throw 'Fake health failure'}}
    # Inspection requests use the actual loopback HTTP server in fake ngrok.exe.
    return Microsoft.PowerShell.Utility\Invoke-RestMethod -Uri $Uri -TimeoutSec $TimeoutSec
}
function Invoke-WebRequest {
    param([string]$Uri,[switch]$UseBasicParsing,[int]$TimeoutSec)
    if($global:NgrokValidationAuthStatus -eq 200){return @{StatusCode=200}}
    $error=[Exception]::new('Fake auth response')
    Add-Member -InputObject $error -MemberType NoteProperty -Name Response -Value ([pscustomobject]@{StatusCode=$global:NgrokValidationAuthStatus})
    throw $error
}
function Run-Case([string]$Mode,[bool]$ExpectedSuccess,[int]$TargetPort=8766,[string]$RequestedUrl='') {
    $inspection=Free-Port
    $config=Join-Path $fixture 'fixture-config.txt'
    [IO.File]::WriteAllText($config, "$inspection|$Mode",[Text.Encoding]::UTF8)
    $message=''
    $accepted=$false
    try {
        $message=& (Join-Path $fixture 'start-ngrok.ps1') -Port $TargetPort -InspectionPort $inspection -ConfigPath $config -StartupTimeoutSeconds 3 -Url $RequestedUrl | Out-String
        $accepted=$true
    }
    catch {$message=$_.Exception.Message}
    if($accepted -ne $ExpectedSuccess){Write-Output ('Fixture safe diagnostic: '+$message.Replace('FAKE_PRIVATE_TOKEN_MARKER','[redacted]'))}
    Assert-Test ($accepted -eq $ExpectedSuccess) ('Scenario '+$Mode+' expected outcome')
    Assert-Test (-not $message.Contains('FAKE_PRIVATE_TOKEN_MARKER')) ('Scenario '+$Mode+' does not reflect log secrets')
    $recordPath=Join-Path $fixture 'outputs\ngrok-process.json'
    $endpointPath=Join-Path $fixture 'outputs\public-endpoint.json'
    if($ExpectedSuccess){
        $record=Get-Content -LiteralPath $recordPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $process=Get-Process -Id $record.pid
        $testProcesses.Add($process)
        $endpoint=Get-Content -LiteralPath $endpointPath -Raw -Encoding UTF8 | ConvertFrom-Json
        Assert-Test ($endpoint.public_url -eq 'https://fixture.ngrok.example' -and $endpoint.port -eq $TargetPort) ('Scenario '+$Mode+' saves real matching HTTPS endpoint')
        Assert-Test ($endpoint.process_association.pid -eq $record.pid -and $endpoint.process_association.start_time_utc_ticks -eq $record.start_time_utc_ticks) ('Scenario '+$Mode+' preserves process association')
        Assert-Test (@($endpoint.PSObject.Properties.Name | Where-Object {$_ -notin @('public_url','verified_at','port','process_association')}).Count -eq 0) ('Scenario '+$Mode+' public metadata whitelist')
        Stop-Process -InputObject $process -Force
        $null=$process.WaitForExit(5000)
        Remove-Item -LiteralPath $recordPath,$endpointPath
    }
    else{
        Assert-Test (-not (Test-Path -LiteralPath $endpointPath)) ('Scenario '+$Mode+' never persists false success')
        Assert-Test (-not (Test-Path -LiteralPath $recordPath)) ('Scenario '+$Mode+' cleans only failed child record')
        if($Mode -eq 'missingtoken'){Assert-Test ($message -match 'authtoken') '4018 safe actionable reason'}
        if($Mode -eq 'limit'){Assert-Test ($message -match 'agent') 'Agent-limit safe actionable reason'}
        if($Mode -eq 'endpointbusy'){
            Assert-Test ($message -match 'ERR_NGROK_334.*域名已被在线隧道占用') '334 endpoint-conflict safe actionable reason'
            Assert-Test ($message.Contains('不要开启pooling连接不同业务')) '334 does not recommend pooling unrelated services'
        }
    }
}

try {
    New-Item -ItemType Directory -Path (Join-Path $fixture 'tools\ngrok') -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $root 'start-ngrok.ps1') -Destination (Join-Path $fixture 'start-ngrok.ps1')
    $tokens=$null;$parseErrors=$null
    [Management.Automation.Language.Parser]::ParseFile((Join-Path $fixture 'start-ngrok.ps1'),[ref]$tokens,[ref]$parseErrors)|Out-Null
    Assert-Test ($parseErrors.Count -eq 0) 'PowerShell5 production script syntax'
    $code=@'
using System;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;
public static class FakeNgrok {
 public static int Main(string[] args) {
  string cfg="";
  for(int i=0;i<args.Length-1;i++)if(args[i]=="--config")cfg=args[i+1];
  string[] bits=File.ReadAllText(cfg).Trim().Split('|');
  int port=int.Parse(bits[0]); string mode=bits[1];
  if(mode=="missingtoken" || mode=="limit" || mode=="endpointbusy") {
   Console.Error.WriteLine(mode=="endpointbusy"?"ERR_NGROK_334 endpoint is already online FAKE_PRIVATE_TOKEN_MARKER":mode=="missingtoken"?"ERR_NGROK_4018 authtoken required FAKE_PRIVATE_TOKEN_MARKER":"ERR_NGROK_108 simultaneous agent session limit FAKE_PRIVATE_TOKEN_MARKER");
   Thread.Sleep(400); return 1;
  }
  if(mode=="nolisten"){Thread.Sleep(15000);return 0;}
  string upstream=args[1];
  if(mode=="wrongport")upstream="http://127.0.0.1:9876";
  TcpListener listener=new TcpListener(IPAddress.Loopback,port);listener.Start();
  int count=0;
  while(true){
   using(TcpClient client=listener.AcceptTcpClient()){
    NetworkStream stream=client.GetStream();
    StreamReader input=new StreamReader(stream,Encoding.ASCII,false,1024,true);
    string line;while(!String.IsNullOrEmpty(line=input.ReadLine())){}
    count++;
    string url=mode=="plainhttp"?"http://fixture.ngrok.example":mode=="secreturl"?"https://fixture.ngrok.example/?token=FAKE_PRIVATE_TOKEN_MARKER":"https://fixture.ngrok.example";
    string protocol=mode=="plainhttp"?"http":"https";
    string body=mode=="deferred" && count<3?"{\"tunnels\":[]}":"{\"tunnels\":[{\"proto\":\""+protocol+"\",\"public_url\":\""+url+"\",\"config\":{\"addr\":\""+upstream+"\"}}]}";
    byte[] payload=Encoding.UTF8.GetBytes(body);
    byte[] header=Encoding.ASCII.GetBytes("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "+payload.Length+"\r\nConnection: close\r\n\r\n");
    stream.Write(header,0,header.Length);stream.Write(payload,0,payload.Length);stream.Flush();
   }
  }
 }
}
'@
    Add-Type -TypeDefinition $code -Language CSharp -OutputAssembly (Join-Path $fixture 'tools\ngrok\ngrok.exe') -OutputType ConsoleApplication
    Run-Case 'success' $true
    Run-Case 'deferred' $true 8765
    foreach($mode in @('wrongport','plainhttp','secreturl','missingtoken','limit','endpointbusy','nolisten')){Run-Case $mode $false}
    Run-Case 'wrong-requested-domain' $false 8766 'https://another.ngrok.example'
    $global:NgrokValidationAuthStatus=200
    Run-Case 'unauthenticated-api' $false
    $global:NgrokValidationAuthStatus=401
    $global:NgrokValidationHealthOk=$false
    Run-Case 'unhealthy-api' $false
    $global:NgrokValidationHealthOk=$true
    $port=Free-Port
    $busy=[Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback,$port)
    try{
        $busy.Start()
        $blocked=$false
        try{$null=& (Join-Path $fixture 'start-ngrok.ps1') -InspectionPort $port -ConfigPath (Join-Path $fixture 'fixture-config.txt')}catch{$blocked=$true}
        Assert-Test $blocked 'Existing inspection listener refused without launching ngrok'
    }finally{$busy.Stop()}
    $recordPath=Join-Path $fixture 'outputs\ngrok-process.json'
    @{pid=$PID;exe_path=(Join-Path $fixture 'tools\ngrok\ngrok.exe');start_time_utc_ticks='1';port=8766}|ConvertTo-Json|Set-Content -LiteralPath $recordPath -Encoding UTF8
    $blocked=$false
    try{$null=& (Join-Path $fixture 'start-ngrok.ps1') -ConfigPath (Join-Path $fixture 'fixture-config.txt')}catch{$blocked=$true}
    Assert-Test ($blocked -and $null -ne (Get-Process -Id $PID)) 'Reused PID or foreign process never stopped'
    Remove-Item -LiteralPath $recordPath
    $blocked=$false
    try{$null=& (Join-Path $fixture 'start-ngrok.ps1') -Port 9999 -ConfigPath (Join-Path $fixture 'fixture-config.txt')}catch{$blocked=$true}
    Assert-Test $blocked 'Only gateway/worker ports 8766/8765 accepted'
    $endpointPath=Join-Path $fixture 'outputs\public-endpoint.json'
    @{public_url='https://do-not-replace.example';process_association=@{pid=$PID;exe_path=(Join-Path $fixture 'tools\ngrok\ngrok.exe');start_time_utc_ticks='1'}} | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath $endpointPath -Encoding UTF8
    $blocked=$false
    try{$null=& (Join-Path $fixture 'start-ngrok.ps1') -ConfigPath (Join-Path $fixture 'fixture-config.txt')}catch{$blocked=$true}
    $kept=Get-Content -LiteralPath $endpointPath -Raw -Encoding UTF8 | ConvertFrom-Json
    Assert-Test ($blocked -and $kept.public_url -eq 'https://do-not-replace.example') 'Unverified existing endpoint metadata never overwritten'
    Remove-Item -LiteralPath $endpointPath
    Write-Output ("Ngrok fake-process/real-loopback-HTTP checks: $($checks.Count)/$($checks.Count) PASS; no real tunnel or real token used.")
}
finally {
    Remove-Variable -Name NgrokValidationHealthOk,NgrokValidationAuthStatus -Scope Global -ErrorAction SilentlyContinue
    foreach($process in $testProcesses){
        $running=Get-Process -Id $process.Id -ErrorAction SilentlyContinue
        if($running -and $running.Path -eq (Join-Path $fixture 'tools\ngrok\ngrok.exe') -and
            $running.StartTime.ToUniversalTime().Ticks -eq $process.StartTime.ToUniversalTime().Ticks){Stop-Process -InputObject $running -Force}
    }
    if(Test-Path -LiteralPath $fixture -PathType Container){
        $checked=[IO.Path]::GetFullPath($fixture).TrimEnd('\')
        if([IO.Path]::GetDirectoryName($checked) -ne $tempRoot -or [IO.Path]::GetFileName($checked) -ne $name -or
            (Get-Item -LiteralPath $checked).Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'Fixture cleanup ownership mismatch; no recursive deletion.'}
        Remove-Item -LiteralPath $checked -Recurse -Force
    }
}
