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

$programFilesRoot = [IO.Path]::GetFullPath($env:ProgramFiles).TrimEnd('\')
$basePython = Join-Path $programFilesRoot 'Python312\python.exe'
if (-not (Test-Path $basePython)) {
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if (-not $launcher) { throw 'Asenna Python 3.12 x64 kaikille käyttäjille (esim. C:\Program Files\Python312) ennen Ilona Agentia.' }
    $pythonBase = (& $launcher.Source -3.12 -c 'import sys; print(sys.base_prefix)' 2>$null | Select-Object -Last 1).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $pythonBase) { throw 'Python Launcher ei löytänyt Python 3.12 -asennusta.' }
    $pythonBase = [IO.Path]::GetFullPath($pythonBase).TrimEnd('\')
    if (-not $pythonBase.StartsWith($programFilesRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "Python 3.12 on asennettu vain käyttäjäkohtaisesti ($pythonBase). Asenna Python kaikille käyttäjille hakemistoon C:\Program Files\Python312 ja suorita asennus uudelleen."
    }
    $basePython = Join-Path $pythonBase 'python.exe'
}
if (-not (Test-Path $basePython)) { throw "Python 3.12 -suoritustiedostoa ei löydy: $basePython" }
& $basePython -c 'import sys; assert sys.version_info[:2] == (3, 12) and sys.maxsize > 2**32'
if ($LASTEXITCODE -ne 0) { throw 'Ilona Agent vaatii 64-bittisen Python 3.12:n asennettuna kaikille käyttäjille.' }
$python = $basePython
& $python -c 'import pip, sysconfig'
if ($LASTEXITCODE -ne 0) {
    throw 'Pythonin pip-moduuli ei ole käytettävissä. Korjaa tai asenna Python 3.12 (kaikille käyttäjille, mukaan lukien pip) ja tarkista Windowsin sovellushallinnan käytännöt.'
}
$pythonBase = (& $basePython -c 'import sys; print(sys.base_prefix)' | Select-Object -Last 1).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Pythonin asennuspolkua ei voitu tarkistaa.' }
$pythonBase = [IO.Path]::GetFullPath($pythonBase).TrimEnd('\')
if (-not $pythonBase.StartsWith($programFilesRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw "Pythonin on oltava kaikkien käyttäjien saatavilla Program Files -hakemistossa ($pythonBase)."
}

New-Item -ItemType Directory -Force -Path $installDir, $dataDir | Out-Null
$venv = Join-Path $installDir 'venv'
$venvPython = Join-Path $venv 'Scripts\python.exe'
$agent = Join-Path $installDir 'ilona_agent_windows.py'
$sitePackages = (& $python -c 'import site; print(site.getsitepackages().__getitem__(0))' | Select-Object -Last 1).Trim()
if ($LASTEXITCODE -ne 0 -or -not $sitePackages) { throw 'Pythonin yhteistä site-packages-hakemistoa ei voitu selvittää.' }
$sitePackages = [IO.Path]::GetFullPath($sitePackages)
if (-not $sitePackages.StartsWith($programFilesRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw "Pythonin site-packages-hakemiston on oltava kaikkien käyttäjien käytettävissä ($sitePackages)."
}

$existingService = Get-Service -Name IlonaAgent -ErrorAction SilentlyContinue
if ($existingService) {
    if ($existingService.Status -ne 'Stopped') {
        Stop-Service -Name IlonaAgent -Force -ErrorAction Stop
        $existingService.WaitForStatus('Stopped', [TimeSpan]::FromSeconds(30))
    }
    if (Test-Path $venvPython) {
        & $venvPython $agent remove
    } else {
        & $python $agent remove
    }
    if ($LASTEXITCODE -ne 0) { throw 'Aiemman Ilona Agent -palvelun poistaminen epäonnistui.' }
}

# pywin32's Python service host does not reliably load modules from a venv.
# Install its sole third-party dependency into machine Python and install the
# importable service module beside it, all under the machine-wide Program Files tree.
& $python -m pip install --disable-pip-version-check -r (Join-Path $source 'requirements-windows.txt')
if ($LASTEXITCODE -ne 0) {
    throw 'pywin32n asennus epäonnistui. Tarkista, sallivatko Windowsin sovellushallinnan käytännöt Pythonin pip-moduulin ja pywin32-paketin.'
}
Copy-Item -Force (Join-Path $source 'ilona_agent_windows.py') $installDir
Copy-Item -Force (Join-Path $source 'ilona_agent_windows.py') $sitePackages
Copy-Item -Force (Join-Path $source 'server-ca.crt') $dataDir

if (Test-Path $venv) {
    Remove-Item -LiteralPath $venv -Recurse -Force
}

# Credentials, local queue and logs are readable only by LocalSystem and local
# Administrators. Program Files keeps its normal Administrators/SYSTEM ACL.
& icacls.exe $dataDir /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Agentin tietohakemiston käyttöoikeuksien asetus epäonnistui.' }

$configPath = Join-Path $dataDir 'config.json'
if (-not $SkipEnrollment -and -not (Test-Path $configPath)) {
    Write-Host "Varmista, että Ilona Adminin HTTPS-osoite on saavutettavissa (VPN-yhteys kunnossa)."
    Write-Host 'Syötä tämän työaseman Ilona Adminissa luotu kertakäyttöinen enrollment-token.'
    & $python $agent --config $configPath --ca-file (Join-Path $dataDir 'server-ca.crt') enroll
    if ($LASTEXITCODE -ne 0) { throw 'Enrollment epäonnistui. Palvelua ei asennettu eikä käynnistetty.' }
} elseif (Test-Path $configPath) {
    Write-Host 'Työasema on jo enrollattu; käytetään tallennettua laitekohtaista tunnistetta.'
}

if (Test-Path $configPath) {
    # pywin32's HandleCommandLine expects options before its service verb.
    & $python $agent --startup auto install
    if ($LASTEXITCODE -ne 0) { throw 'Windows-palvelun asennus epäonnistui.' }
    & $python $agent start
    if ($LASTEXITCODE -ne 0) { throw 'Windows-palvelun käynnistys epäonnistui.' }
    $service = Get-Service -Name IlonaAgent -ErrorAction Stop
    $deadline = (Get-Date).AddSeconds(30)
    do {
        $service.Refresh()
        if ($service.Status -eq 'Running') { break }
        Start-Sleep -Seconds 1
    } while ((Get-Date) -lt $deadline)
    if ($service.Status -ne 'Running') {
        throw "Ilona Agent -palvelu ei käynnistynyt (tila: $($service.Status)). Tarkista Event Viewer: Windows Logs > Application ja System."
    }
    Write-Host 'Ilona Agent -palvelu asennettiin ja käynnistettiin.'
} else {
    Write-Host 'Agentti asennettiin. Enrollment puuttuu; aja enroll ja asenna sen jälkeen palvelu.'
}

Write-Host "Lokitiedosto: $(Join-Path $dataDir 'agent.log')"
Write-Host 'Palvelu: IlonaAgent (LocalSystem; ei käyttöliittymää; konekohtainen Python)'
