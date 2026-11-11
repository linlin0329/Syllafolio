param(
    [string]$Output = (Join-Path $PSScriptRoot 'Syllafolio.exe'),
    [string]$Icon = (Join-Path $PSScriptRoot '..\branding\Syllafolio.ico'),
    [switch]$WithoutIcon
)
$ErrorActionPreference = 'Stop'
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $compiler -PathType Leaf)) { throw 'The local .NET Framework x64 C# compiler was not found.' }
$outputPath = [IO.Path]::GetFullPath($Output)
$arguments = @('/nologo', '/target:winexe', '/platform:x64', '/optimize+', '/warnaserror+', '/debug-', '/reference:System.Windows.Forms.dll', '/reference:System.Drawing.dll', ('/out:' + $outputPath), ('/win32manifest:' + (Join-Path $PSScriptRoot 'app.manifest')))
if (-not $WithoutIcon) {
    if (-not (Test-Path -LiteralPath $Icon -PathType Leaf)) { throw "Icon not found: $Icon" }
    $arguments += '/win32icon:' + [IO.Path]::GetFullPath($Icon)
}
$arguments += Join-Path $PSScriptRoot 'SyllafolioLauncher.cs'
& $compiler @arguments
if ($LASTEXITCODE -ne 0) { throw "C# compiler failed: $LASTEXITCODE" }
$file = Get-Item -LiteralPath $outputPath
[pscustomobject]@{path=$file.FullName; bytes=$file.Length; sha256=(Get-FileHash -LiteralPath $outputPath -Algorithm SHA256).Hash.ToLower(); subsystem='Windows GUI'; architecture='x64'; compiler=$compiler; icon=if($WithoutIcon){$null}else{[IO.Path]::GetFullPath($Icon)}} | ConvertTo-Json
