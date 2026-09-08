$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectDir

$Python = if ($env:PYTHON_BIN) { $env:PYTHON_BIN } else { "python" }
& $Python -m PyInstaller --noconfirm --clean TalentMiner.spec

if ($env:WINDOWS_CERT_PATH) {
    $SignTool = Get-Command signtool -ErrorAction SilentlyContinue
    if (-not $SignTool) { throw "signtool was not found" }
    & $SignTool.Source sign /fd SHA256 /td SHA256 /tr http://timestamp.digicert.com `
        /f $env:WINDOWS_CERT_PATH /p $env:WINDOWS_CERT_PASSWORD dist/TalentMiner.exe
}

New-Item -ItemType Directory -Force -Path release | Out-Null
$Iscc = Get-Command iscc -ErrorAction SilentlyContinue
if (-not $Iscc) {
    throw "Inno Setup is required. Install it with: choco install innosetup"
}
& $Iscc.Source "packaging/windows-installer.iss"
if ($LASTEXITCODE -ne 0) {
    throw "Inno Setup failed with exit code $LASTEXITCODE"
}

if ($env:WINDOWS_CERT_PATH) {
    & $SignTool.Source sign /fd SHA256 /td SHA256 /tr http://timestamp.digicert.com `
        /f $env:WINDOWS_CERT_PATH /p $env:WINDOWS_CERT_PASSWORD `
        release/TalentMiner-Windows-x64-Setup.exe
}
