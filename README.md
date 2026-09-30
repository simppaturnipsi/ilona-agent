# Ilona Agent

Ilona Agent is a headless workstation service with no user interface. Ubuntu
uses the systemd user service below; Windows uses the `IlonaAgent` Windows
service running as LocalSystem. Both platforms use the same versioned HTTPS
API, one-use enrollment token, per-workstation credential, report cadence and
offline SQLite WAL queue.

## Windows 11

The Windows agent collects computer/BIOS identity, CPU, RAM modules, physical
disks and available reliability counters, fixed volumes, batteries, network
adapters, domain trust status, and installed applications from the Windows
uninstall registry for the machine and loaded user profiles. It avoids
`Win32_Product` so inventory does not trigger Windows Installer repairs.
Unsupported SMART, battery, or other readings stay unknown rather than being
reported as zero.

OS inventory includes the Windows product caption/edition (including Windows
11 Pro and Windows 11 Education), DisplayVersion (for example 24H2), build and
UBR revision, architecture, install date, last boot and uptime. The agent does
not read or transmit Windows product keys; edition is identified from Windows
system metadata. Installed application versions are reported separately.

Requirements: 64-bit Python 3.12 with the Python Launcher, PowerShell, and an
Administrator PowerShell session. First establish the workstation's VPN route
to Ilona Admin. Download and extract `IlonaAgent-Windows.zip`, then open an
Administrator PowerShell window in the folder containing `install-windows.ps1`:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\install-windows.ps1
```

Create the workstation record in Ilona Admin and generate its one-use
enrollment token first. The installer prompts for that token without echoing
it, validates the supplied Ilona CA certificate, enrolls this workstation,
sets the credential/config/queue directory ACL to LocalSystem and local
Administrators, then installs and starts the `IlonaAgent` service. The
machine-specific API credential is stored under
`C:\ProgramData\Ilona\Agent\config.json`; it is not written to SQLite.

Service status and logs:

```powershell
Get-Service IlonaAgent
Get-Content "$env:ProgramData\Ilona\Agent\agent.log" -Tail 100
```

The service collects immediately after it starts, sends heartbeat every
minute, health hourly and full inventory daily. Intervals are stored in the
protected config file. Its local SQLite queue is retained across VPN/server
outages and drained when connectivity returns. Re-enrollment is not needed for
ordinary outages. To install without enrolling, use
`install-windows.ps1 -SkipEnrollment`; after manually enrolling with the
installed Python command, run `& "$env:ProgramFiles\Ilona\Agent\venv\Scripts\python.exe"
"$env:ProgramFiles\Ilona\Agent\ilona_agent_windows.py" --startup auto install` and
then run the same command with `start` in place of `install`.

The Windows installer does not install or alter WireGuard, domain membership,
Windows licensing, or any other workstation service. Those remain part of the
existing workstation provisioning process.

For deferred enrollment, first prepare the machine's record and token in Admin,
then run the following from an elevated PowerShell window after the
`-SkipEnrollment` install:

```powershell
$py = "$env:ProgramFiles\Ilona\Agent\venv\Scripts\python.exe"
$agent = "$env:ProgramFiles\Ilona\Agent\ilona_agent_windows.py"
$data = "$env:ProgramData\Ilona\Agent"
& $py $agent --config "$data\config.json" --ca-file "$data\server-ca.crt" enroll
& $py $agent --startup auto install
& $py $agent start
```

## Ubuntu

Headless Python 3 systemd user service for Ubuntu. It collects OS, CPU, RAM,
firmware, disk/filesystem, battery, network, SSSD/realm and dpkg inventory.
Optional SMART readings are collected when `smartctl` is already installed.
Unavailable readings are sent as null or omitted; the agent does not install
packages or invoke privileged commands.
Requirements are Python 3.10 or newer and systemd user services; the agent uses
only the Python standard library.

The local SQLite queue uses WAL, coalesces pending heartbeats, and retries
reports after network outages. Inventory is sent at startup and daily, health
hourly, and heartbeat every minute. All intervals can be set in
`~/.config/ilona-agent/config.json`.

## Install and enroll

Download and extract `ilona-agent-ubuntu.tar.gz`, then run `./install-user.sh`
from the extracted directory. It installs the agent under `~/.local`, installs and
reloads a systemd user unit, and installs the bundled Ilona Admin CA certificate.
The certificate is self-signed and
the agent validates it for HTTPS; it never disables TLS verification.

In Ilona Admin, create the workstation record and generate its one-time
enrollment token. Then run `~/.local/bin/ilona-agent enroll` and paste the
token at the hidden prompt. Enrollment creates a per-device credential; only
that credential's hash is retained by the server. The local JSON file is mode
0600. The command starts the user service after successful enrollment.

Check with `systemctl --user status ilona-agent` and
`journalctl --user -u ilona-agent`. A one-shot diagnostic is
`~/.local/bin/ilona-agent --log-level DEBUG once`.

The installer does not change system-wide settings. For this workstation,
systemd user lingering has been enabled so the service remains available before
login and after logout. On another workstation, enabling that behavior requires
`sudo loginctl enable-linger <username>` or a root-managed system service.

## Downloads

Installers are published as GitHub Release assets:

- Windows: `IlonaAgent-Windows.zip`
- Ubuntu: `ilona-agent-ubuntu.tar.gz`

The downloads contain the platform agent, installer script, and Ilona Admin CA
certificate. Before enrollment, create the workstation record in Ilona Admin
and ensure the machine can reach its HTTPS API over the Ilona VPN. Enrollment
tokens are one-use and should not be placed in scripts or shared with others.
