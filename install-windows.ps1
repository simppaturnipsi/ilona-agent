[CmdletBinding()]
param(
    [string]$ApiUrl = 'https://ilona.ad.vapepa.info:10443',
    [switch]$SkipEnrollment
)

$ErrorActionPreference = 'Stop'
$source = Split-Path -Parent $MyInvocation.MyCommand.Path
$installDir = Join-Path $env:ProgramFiles 'Ilona\Agent'
$dataDir = Join-Path $env:ProgramData 'Ilona\Agent'

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Suorita asennus PowerShellissä järjestelmänvalvojana.'
}

$launcher = Get-Command py.exe -ErrorAction SilentlyContinue
if (-not $launcher) { throw 'Asenna ensin Python 3.12 x64 kaikille käyttäjille ja Python Launcher (py.exe).' }
& $launcher.Source -3.12 -c 'import sys; assert sys.maxsize > 2**32' 2>$null
if ($LASTEXITCODE -ne 0) { throw 'Ilona Agent vaatii 64-bittisen Python 3.12 -asennuksen.' }

New-Item -ItemType Directory -Force -Path $installDir, $dataDir | Out-Null
$venv = Join-Path $installDir 'venv'
if (-not (Test-Path (Join-Path $venv 'Scripts\python.exe'))) {
    & $launcher.Source -3.12 -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw 'Python-virtuaaliympäristön luonti epäonnistui.' }
}
$python = Join-Path $venv 'Scripts\python.exe'
& $python -m pip install --disable-pip-version-check -r (Join-Path $source 'requirements-windows.txt')
if ($LASTEXITCODE -ne 0) { throw 'pywin32-riippuvuuden asennus epäonnistui.' }
Copy-Item -Force (Join-Path $source 'ilona_agent_windows.py') $installDir
Copy-Item -Force (Join-Path $source 'server-ca.crt') $dataDir

# Credentials, local queue and logs are readable only by LocalSystem and local
# Administrators. Program Files keeps its normal Administrators/SYSTEM ACL.
& icacls.exe $dataDir /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Agentin tietohakemiston käyttöoikeuksien asetus epäonnistui.' }

$agent = Join-Path $installDir 'ilona_agent_windows.py'
if (-not $SkipEnrollment) {
    Write-Host "Varmista, että Ilona Adminin HTTPS-osoite on saavutettavissa (VPN-yhteys kunnossa)."
    Write-Host 'Syötä tämän työaseman Ilona Adminissa luotu kertakäyttöinen enrollment-token.'
    & $python $agent --config (Join-Path $dataDir 'config.json') --ca-file (Join-Path $dataDir 'server-ca.crt') enroll
    if ($LASTEXITCODE -ne 0) { throw 'Enrollment epäonnistui. Palvelua ei asennettu eikä käynnistetty.' }
}

if (-not $SkipEnrollment -or (Test-Path (Join-Path $dataDir 'config.json'))) {
    & $python $agent install --startup auto
    if ($LASTEXITCODE -ne 0) { throw 'Windows-palvelun asennus epäonnistui.' }
    & $python $agent start
    if ($LASTEXITCODE -ne 0) { throw 'Windows-palvelun käynnistys epäonnistui.' }
    Write-Host 'Ilona Agent -palvelu asennettiin ja käynnistettiin.'
} else {
    Write-Host 'Agentti asennettiin. Enrollment puuttuu; aja enroll ja asenna sen jälkeen palvelu.'
}

Write-Host "Lokitiedosto: $(Join-Path $dataDir 'agent.log')"
Write-Host 'Palvelu: IlonaAgent (LocalSystem; ei käyttöliittymää)'
