#!/usr/bin/env python
"""Headless Windows service agent for Ilona Admin workstation inventory."""
import argparse
import getpass
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import platform
import re
import signal
import socket
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

VERSION = '0.2.3'
ROOT = Path(os.environ.get('PROGRAMDATA', r'C:\ProgramData')) / 'Ilona' / 'Agent'
CONFIG = ROOT / 'config.json'
CA_FILE = ROOT / 'server-ca.crt'
STATE = ROOT / 'queue.db'
LOG_FILE = ROOT / 'agent.log'
STOP = False
SERVICE_COMMANDS = frozenset(('install', 'update', 'remove', 'start', 'stop', 'restart', 'debug'))


def is_service_command(arguments):
    """Recognize pywin32 service verbs even when options precede the verb."""
    return any(argument in SERVICE_COMMANDS for argument in arguments)


def powershell_json(expression, timeout=45):
    """Run a read-only PowerShell inventory query and decode its JSON output."""
    script = ("$ErrorActionPreference='SilentlyContinue'; $utf8=New-Object System.Text.UTF8Encoding; "
              "[Console]::OutputEncoding=$utf8; $OutputEncoding=$utf8; "
              f"@({expression}) | ConvertTo-Json -Depth 7 -Compress")
    try:
        result = subprocess.run(['powershell.exe', '-NoLogo', '-NoProfile', '-NonInteractive',
                                 '-ExecutionPolicy', 'Bypass', '-Command', script],
                                capture_output=True, text=True, timeout=timeout, check=False,
                                encoding='utf-8', errors='replace')
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0 or not result.stdout.strip():
        return []
    try:
        value = json.loads(result.stdout)
        return value if isinstance(value, list) else [value]
    except (ValueError, TypeError):
        return []


def one(expression):
    values = powershell_json(expression)
    return values[0] if values and isinstance(values[0], dict) else {}


def int_or_none(value):
    try:
        return int(value) if value not in (None, '') else None
    except (TypeError, ValueError, OverflowError):
        return None


def number_or_none(value):
    try:
        return float(value) if value not in (None, '') else None
    except (TypeError, ValueError, OverflowError):
        return None


def timestamp_or_none(value):
    if not value:
        return None
    try:
        # PowerShell emits ISO-8601 UTC for CIM DateTime values.
        from datetime import datetime, timezone
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    except (ValueError, TypeError, OverflowError):
        return None


def _version_info():
    return one("Get-ItemProperty 'HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion' | "
               "Select-Object ProductName,EditionID,DisplayVersion,ReleaseId,CurrentBuild,UBR,InstallationType")


def _install_date(value):
    if not value:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).date().isoformat()
    except ValueError:
        return str(value)[:10] if re.match(r'^\d{4}-\d{2}-\d{2}', str(value)) else None


def device_data():
    cs = one('Get-CimInstance Win32_ComputerSystem | Select-Object Manufacturer,Model,Domain,PartOfDomain,DNSHostName')
    osdata = one('Get-CimInstance Win32_OperatingSystem | Select-Object Caption,Version,BuildNumber,OSArchitecture,'
        '@{n="InstallDate";e={if($_.InstallDate){$_.InstallDate.ToUniversalTime().ToString("o")}}},'
        '@{n="LastBootUpTime";e={if($_.LastBootUpTime){$_.LastBootUpTime.ToUniversalTime().ToString("o")}}}')
    ver = _version_info()
    bios = one('Get-CimInstance Win32_BIOS | Select-Object Manufacturer,SMBIOSBIOSVersion,SerialNumber,'
        '@{n="ReleaseDate";e={if($_.ReleaseDate){$_.ReleaseDate.ToUniversalTime().ToString("o")}}}')
    # Preserve edition/SKU explicitly (including Windows 11 Pro and Education).
    product = osdata.get('Caption') or ver.get('ProductName')
    edition = ver.get('EditionID')
    if product and edition and not any(x in str(product).lower() for x in ('education', 'professional', 'pro')):
        if edition.lower() in ('education', 'professional'):
            product = f'{product} {"Education" if edition.lower() == "education" else "Pro"}'
    display_version = ver.get('DisplayVersion') or ver.get('ReleaseId')
    numeric_version = osdata.get('Version')
    version = (f'{display_version} ({numeric_version})' if display_version and numeric_version else
               display_version or numeric_version)
    build = osdata.get('BuildNumber') or ver.get('CurrentBuild')
    ubr = int_or_none(ver.get('UBR'))
    if build and ubr is not None:
        build = f'{build}.{ubr}'
    boot = osdata.get('LastBootUpTime')
    booted_at = timestamp_or_none(boot)
    uptime = max(0, int(time.time()) - booted_at) if booted_at is not None else None
    return cs, osdata, ver, bios, {
        'manufacturer': cs.get('Manufacturer'), 'model': cs.get('Model'),
        'serial_number': bios.get('SerialNumber'), 'asset_tag': None,
        'architecture': osdata.get('OSArchitecture') or platform.machine(),
        'os': {'family': 'Windows', 'product_name': product, 'version': str(version) if version else None,
               'build': str(build) if build else None, 'kernel': None,
               'architecture': osdata.get('OSArchitecture') or platform.machine(),
               'install_date': _install_date(osdata.get('InstallDate')),
               'booted_at': booted_at, 'uptime_seconds': uptime},
        'bios': {'manufacturer': bios.get('Manufacturer'),
                 'version': bios.get('SMBIOSBIOSVersion'),
                 'release_date': _install_date(bios.get('ReleaseDate'))}}


def cpu_data():
    item = one('Get-CimInstance Win32_Processor | Select-Object -First 1 Manufacturer,Name,NumberOfCores,NumberOfLogicalProcessors')
    return {'vendor': item.get('Manufacturer'), 'model': item.get('Name'),
            'physical_cores': int_or_none(item.get('NumberOfCores')),
            'logical_processors': int_or_none(item.get('NumberOfLogicalProcessors'))}


def memory_data():
    modules = powershell_json('Get-CimInstance Win32_PhysicalMemory | Where-Object Capacity -gt 0 | '
        'Select-Object @{n="size_bytes";e={[int64]$_.Capacity}},@{n="manufacturer";e={$_.Manufacturer.Trim()}},'
        '@{n="speed_mhz";e={[int]$_.Speed}},@{n="serial_number";e={$_.SerialNumber.Trim()}},'
        '@{n="part_number";e={$_.PartNumber.Trim()}}')
    modules = [{'size_bytes': int_or_none(x.get('size_bytes')), 'manufacturer': x.get('manufacturer') or None,
                'speed_mhz': int_or_none(x.get('speed_mhz')), 'serial_number': x.get('serial_number') or None,
                'part_number': x.get('part_number') or None} for x in modules if isinstance(x, dict)]
    total = sum(x['size_bytes'] for x in modules if x.get('size_bytes') is not None) or None
    if total is None:
        total = int_or_none(one('Get-CimInstance Win32_OperatingSystem | Select-Object TotalVisibleMemorySize').get('TotalVisibleMemorySize'))
        total = total * 1024 if total else None
    return total, modules


def storage_data():
    # Win32_DiskDrive includes physical devices only; removable USB media are also
    # reported as devices, with unknown health metrics left null.
    disks = powershell_json('Get-CimInstance Win32_DiskDrive | Select-Object Index,Manufacturer,Model,SerialNumber,InterfaceType,MediaType,Size,PNPDeviceID')
    physical = powershell_json('Get-PhysicalDisk | Select-Object FriendlyName,SerialNumber,MediaType,BusType,HealthStatus,OperationalStatus,Size')
    reliability = powershell_json('Get-PhysicalDisk | ForEach-Object { $d=$_; $r=Get-StorageReliabilityCounter -PhysicalDisk $d; '
        '[pscustomobject]@{SerialNumber=$d.SerialNumber;FriendlyName=$d.FriendlyName;Temperature=$r.Temperature;'
        'PowerOnHours=$r.PowerOnHours;Wear=$r.Wear;ReadErrorsUncorrected=$r.ReadErrorsUncorrected;WriteErrorsUncorrected=$r.WriteErrorsUncorrected} }')
    by_serial = {str(x.get('SerialNumber') or '').strip(): x for x in physical if isinstance(x, dict)}
    rel_by_serial = {str(x.get('SerialNumber') or '').strip(): x for x in reliability if isinstance(x, dict)}
    result = []
    for disk in disks:
        if not isinstance(disk, dict):
            continue
        serial = str(disk.get('SerialNumber') or '').strip()
        p = by_serial.get(serial, {})
        rel = rel_by_serial.get(serial, {})
        media = str(p.get('MediaType') or disk.get('MediaType') or '').lower()
        bus = str(p.get('BusType') or '').lower()
        desc = f"{disk.get('Model') or ''} {disk.get('PNPDeviceID') or ''}".lower()
        kind = 'NVME' if 'nvme' in bus or 'nvme' in desc else (
            'HDD' if media == 'hdd' or 'hard disk' in media else (
                'SATA_SSD' if media == 'ssd' else 'UNKNOWN'))
        status = str(p.get('HealthStatus') or '').lower()
        smart = 'OK' if status == 'healthy' else ('WARNING' if status else None)
        wear = number_or_none(rel.get('Wear'))
        result.append({'id': str(disk.get('Index')), 'manufacturer': disk.get('Manufacturer') or None,
            'model': disk.get('Model') or None, 'serial_number': serial or None, 'kind': kind,
            'capacity_bytes': int_or_none(disk.get('Size')), 'smart_status': smart,
            'temperature_c': number_or_none(rel.get('Temperature')),
            'power_on_hours': int_or_none(rel.get('PowerOnHours')), 'wear_percent': wear,
            'available_spare_percent': None, 'percentage_used': None,
            'health': {'health_status': p.get('HealthStatus'), 'operational_status': p.get('OperationalStatus'),
                       'read_errors_uncorrected': int_or_none(rel.get('ReadErrorsUncorrected')),
                       'write_errors_uncorrected': int_or_none(rel.get('WriteErrorsUncorrected'))}})
    return result


def filesystem_data(storage):
    volumes = powershell_json('Get-CimInstance Win32_LogicalDisk -Filter "DriveType=3" | '
        'Select-Object DeviceID,FileSystem,Size,FreeSpace')
    result = []
    for item in volumes:
        total, free = int_or_none(item.get('Size')), int_or_none(item.get('FreeSpace'))
        if not total or free is None:
            continue
        result.append({'storage_id': None, 'mount_point': item.get('DeviceID'), 'filesystem': item.get('FileSystem'),
            'capacity_bytes': total, 'used_bytes': max(0, total - free), 'free_bytes': free,
            'free_percent': round(free / total * 100, 2)})
    return result


def battery_data():
    rows = powershell_json('Get-CimInstance Win32_Battery | Select-Object DeviceID,Name,Manufacturer,SerialNumber,DesignCapacity,FullChargedCapacity,BatteryStatus')
    full_rows = powershell_json('Get-CimInstance -Namespace root\\wmi -ClassName BatteryFullChargedCapacity | Select-Object InstanceName,FullChargedCapacity')
    static_rows = powershell_json('Get-CimInstance -Namespace root\\wmi -ClassName BatteryStaticData | Select-Object InstanceName,DesignedCapacity,ManufactureName,DeviceName,SerialNumber')
    status_rows = powershell_json('Get-CimInstance -Namespace root\\wmi -ClassName BatteryStatus | Select-Object InstanceName,PowerOnline,Charging,Discharging,RemainingCapacity')
    full = {str(x.get('InstanceName','')).lower(): x for x in full_rows if isinstance(x,dict)}
    static = {str(x.get('InstanceName','')).lower(): x for x in static_rows if isinstance(x,dict)}
    statuses = {str(x.get('InstanceName','')).lower(): x for x in status_rows if isinstance(x,dict)}
    result = []
    for row in rows:
        instance = str(row.get('DeviceID') or '').lower()
        st = next((v for k,v in static.items() if instance and instance in k), {})
        fc = next((v for k,v in full.items() if instance and instance in k), {})
        state = next((v for k,v in statuses.items() if instance and instance in k), {})
        design = int_or_none(st.get('DesignedCapacity')) or int_or_none(row.get('DesignCapacity'))
        charged = int_or_none(fc.get('FullChargedCapacity')) or int_or_none(row.get('FullChargedCapacity'))
        current = int_or_none(state.get('RemainingCapacity'))
        health = round(charged / design * 100, 2) if charged is not None and design else None
        charging = state.get('Charging')
        discharging = state.get('Discharging')
        charge_state = 'CHARGING' if charging is True else ('DISCHARGING' if discharging is True else
                      ('FULL' if int_or_none(row.get('BatteryStatus')) == 2 else None))
        result.append({'manufacturer': st.get('ManufactureName') or row.get('Manufacturer') or None,
            'model': st.get('DeviceName') or row.get('Name') or None,
            'serial_number': st.get('SerialNumber') or row.get('SerialNumber') or None,
            'design_capacity_mwh': design, 'full_charge_capacity_mwh': charged,
            'current_capacity_mwh': current, 'cycle_count': None,
            'status': charge_state, 'health_percent': health})
    return result


def network_data():
    adapters = powershell_json('Get-NetAdapter -IncludeHidden | Select-Object Name,InterfaceDescription,MacAddress,Status,MediaType,PhysicalMediaType,HardwareInterface')
    addresses = powershell_json('Get-NetIPAddress | Where-Object {$_.AddressState -eq "Preferred"} | Select-Object InterfaceAlias,AddressFamily,IPAddress')
    by_name = {}
    for item in addresses:
        by_name.setdefault(str(item.get('InterfaceAlias') or '').lower(), []).append(item)
    result = []
    for item in adapters:
        name = str(item.get('Name') or '')
        desc = str(item.get('InterfaceDescription') or '')
        found = by_name.get(name.lower(), [])
        ipv4 = next((x.get('IPAddress') for x in found if x.get('AddressFamily') == 2), None)
        ipv6 = next((x.get('IPAddress') for x in found if x.get('AddressFamily') == 23 and not str(x.get('IPAddress','')).startswith('fe80:')), None)
        vpn = bool(re.search(r'wireguard|wintun|tap-windows|vpn', name + ' ' + desc, re.I))
        typ = 'vpn' if vpn else ('wireless' if 'wi-fi' in desc.lower() or 'wireless' in desc.lower() else
              ('ethernet' if item.get('HardwareInterface') else str(item.get('MediaType') or 'virtual').lower()))
        result.append({'name': name or desc, 'interface_type': typ, 'mac_address': item.get('MacAddress'),
            'ipv4': ipv4, 'ipv6': ipv6, 'link_state': item.get('Status'), 'is_vpn': int(vpn)})
    return result


def domain_data(cs):
    joined = bool(cs.get('PartOfDomain'))
    domain = cs.get('Domain') if joined else None
    state = None
    if joined and domain:
        check = subprocess.run(['powershell.exe', '-NoLogo', '-NoProfile', '-NonInteractive',
             '-ExecutionPolicy', 'Bypass', '-Command', 'Test-ComputerSecureChannel -Quiet'],
             capture_output=True, text=True, timeout=30, check=False)
        state = 'ONLINE' if check.returncode == 0 and check.stdout.strip().lower() == 'true' else 'OFFLINE'
    return {'joined': int(joined), 'domain_name': domain,
            'computer_account': (str(cs.get('DNSHostName') or socket.gethostname()) + '$') if joined else None,
            'connection_state': state if joined else 'NOT_JOINED'}


def software_data():
    # Both registry views are queried; Win32_Product is intentionally avoided
    # because querying it can trigger MSI repair actions.
    script = r'''
    $paths=@('HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*',
             'HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*')
    $hives=Get-ChildItem 'Registry::HKEY_USERS' | Where-Object {$_.PSChildName -match '^S-1-5-21-\d+-\d+-\d+-\d+$'}
    foreach($hive in $hives) {
      $sid=$hive.PSChildName
      $paths+=@("Registry::HKEY_USERS\$sid\Software\Microsoft\Windows\CurrentVersion\Uninstall\*",
                "Registry::HKEY_USERS\$sid\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*")
    }
    foreach($path in $paths) { Get-ItemProperty $path | Where-Object {$_.DisplayName} |
      Select-Object @{n='source';e={'windows_registry'}},@{n='name';e={$_.DisplayName}},
      @{n='version';e={$_.DisplayVersion}},@{n='publisher';e={$_.Publisher}},
      @{n='install_date';e={if($_.InstallDate -match '^\d{8}$'){ '{0}-{1}-{2}' -f $_.InstallDate.Substring(0,4),$_.InstallDate.Substring(4,2),$_.InstallDate.Substring(6,2) } else {$null}}} }
    '''
    rows = powershell_json(script, timeout=90)
    result = []
    seen = set()
    for row in rows:
        name = str(row.get('name') or '').strip()
        version = str(row.get('version') or '').strip()
        if not name:
            continue
        key = (name.lower(), version.lower())
        if key in seen:
            continue
        seen.add(key)
        result.append({'source': 'windows_registry', 'name': name[:512],
            'version': version[:512] or None,
            'publisher': (str(row.get('publisher') or '').strip()[:512] or None),
            'install_date': row.get('install_date')})
        if len(result) >= 5000:
            break
    return result


def collect_inventory(include_software=True):
    cs, osdata, ver, bios, base = device_data()
    memory_total, modules = memory_data()
    storage = storage_data()
    base.update({'agent_version': VERSION, 'hostname': str(cs.get('DNSHostName') or socket.gethostname()),
        'cpu': cpu_data(), 'domain': domain_data(cs), 'memory_total_bytes': memory_total,
        'memory_modules': modules, 'storage_devices': storage, 'filesystems': filesystem_data(storage),
        'batteries': battery_data(), 'network_interfaces': network_data(),
        'installed_software': software_data() if include_software else []})
    return base


def health_payload(inventory=None):
    inventory = inventory or collect_inventory(include_software=False)
    batteries = [x['health_percent'] for x in inventory.get('batteries', []) if x.get('health_percent') is not None]
    free = [x['free_percent'] for x in inventory.get('filesystems', []) if x.get('free_percent') is not None]
    disks = inventory.get('storage_devices', [])
    smart = [x.get('smart_status') for x in disks if x.get('smart_status')]
    wear = [x.get('percentage_used') for x in disks if isinstance(x.get('percentage_used'), (int, float))]
    state = 'WARNING' if any(x in ('WARNING','FAILED') for x in smart) or (free and min(free) < 10) else 'OK'
    if batteries and min(batteries) < 70:
        state = 'WARNING'
    return {'battery_health_percent': min(batteries) if batteries else None, 'overall_state': state,
        'measurements': {'smart_status': 'FAILED' if 'FAILED' in smart else ('WARNING' if 'WARNING' in smart else ('OK' if 'OK' in smart else None)),
        'minimum_free_percent': min(free) if free else None,
        'maximum_percentage_used': max(wear) if wear else None}}


class Queue:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=15)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA busy_timeout=15000')
        self.db.execute('CREATE TABLE IF NOT EXISTS reports(id INTEGER PRIMARY KEY,kind TEXT NOT NULL,payload TEXT NOT NULL,created_at INTEGER NOT NULL)')
        self.db.execute('CREATE INDEX IF NOT EXISTS ix_reports_created ON reports(id)')
        self.db.commit()
    def enqueue(self, kind, payload):
        with self.db:
            if kind == 'heartbeat':
                self.db.execute('DELETE FROM reports WHERE kind=?', (kind,))
            self.db.execute('INSERT INTO reports(kind,payload,created_at) VALUES(?,?,?)',
                (kind,json.dumps(payload,separators=(',',':')),int(time.time())))
            self.db.execute('DELETE FROM reports WHERE id NOT IN (SELECT id FROM reports ORDER BY id DESC LIMIT 10000)')
    def pending(self):
        return self.db.execute('SELECT id,kind,payload FROM reports ORDER BY id LIMIT 100').fetchall()
    def acknowledge(self, row_id):
        with self.db:
            self.db.execute('DELETE FROM reports WHERE id=?', (row_id,))
    def close(self):
        self.db.close()


def api_request(url, path, payload=None, credential=None, ca_file=None, method='POST'):
    data = json.dumps(payload,separators=(',',':')).encode() if payload is not None else None
    headers={'User-Agent': f'Ilona-Agent/{VERSION}'}
    if data is not None: headers['Content-Type']='application/json'
    if credential: headers['Authorization']='Bearer '+credential
    req=urllib.request.Request(url.rstrip('/')+path,data=data,headers=headers,method=method)
    context=ssl.create_default_context(cafile=str(ca_file) if ca_file else None)
    with urllib.request.urlopen(req,timeout=30,context=context) as response:
        raw=response.read(2*1024*1024)
        return response.status,json.loads(raw) if raw else {}


def enroll(url, config_path, ca_file=None):
    token=getpass.getpass('Ilonan kertakäyttöinen enrollment-token: ').strip()
    if not token: raise SystemExit('Enrollment-token puuttuu.')
    try:
        _,caps=api_request(url,'/api/v1/workstations/capabilities',ca_file=ca_file,method='GET')
        supports_memory='memory_total_bytes' in caps.get('inventory_fields',[])
    except urllib.error.HTTPError:
        supports_memory=False
    except (urllib.error.URLError,TimeoutError,OSError) as exc:
        raise SystemExit(f'HTTPS-yhteys epäonnistui ({type(exc).__name__}).') from None
    try:
        status,result=api_request(url,'/api/v1/workstations/enroll',{'token':token,'agent_version':VERSION},ca_file=ca_file)
    except urllib.error.HTTPError as exc:
        raise SystemExit(f'Enrollment epäonnistui (HTTP {exc.code}).') from None
    except (urllib.error.URLError,TimeoutError,OSError) as exc:
        raise SystemExit(f'HTTPS-yhteys epäonnistui ({type(exc).__name__}).') from None
    if status!=201 or not result.get('workstation_id') or not result.get('agent_credential'):
        raise SystemExit(f'Enrollment epäonnistui (HTTP {status}).')
    cfg={'server_url':url.rstrip('/'),'workstation_id':result['workstation_id'],'agent_credential':result['agent_credential'],
         'heartbeat_interval_seconds':60,'health_interval_seconds':3600,'inventory_interval_seconds':86400,
         'state_database':str(STATE),'supports_memory_total':supports_memory,'ca_bundle':str(ca_file) if ca_file else None}
    config_path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix='.config-',dir=config_path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as stream:
            json.dump(cfg,stream,indent=2); stream.write('\n')
        os.replace(tmp,config_path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)
    print(f'Enrollment valmis: {result["workstation_id"]}')
    print(f'Tunnistetiedosto tallennettu: {config_path}')


def load_config(path):
    config=json.loads(Path(path).read_text(encoding='utf-8'))
    for key in ('server_url','workstation_id','agent_credential'):
        if not isinstance(config.get(key),str) or not config[key]: raise ValueError('Agent configuration is incomplete')
    config['workstation_id']=str(uuid.UUID(config['workstation_id']))
    if not config['server_url'].startswith('https://'): raise ValueError('API URL must use HTTPS')
    if len(config['agent_credential'])<32: raise ValueError('Agent credential is invalid')
    return config


def flush(queue,config):
    for row_id,kind,payload in queue.pending():
        try:
            api_request(config['server_url'],f"/api/v1/workstations/{config['workstation_id']}/{kind}",
                json.loads(payload),config['agent_credential'],config.get('ca_bundle'))
            queue.acknowledge(row_id)
            logging.info('Delivered %s report',kind)
        except (urllib.error.URLError,TimeoutError,OSError,ValueError) as exc:
            logging.warning('Report remains queued (%s)',type(exc).__name__)
            return False
    return True


def run_agent(config_path, once=False):
    config=load_config(config_path)
    queue=Queue(config.get('state_database') or STATE)
    intervals={'heartbeat':max(30,int(config.get('heartbeat_interval_seconds',60))),
        'health':max(300,int(config.get('health_interval_seconds',3600))),
        'inventory':max(3600,int(config.get('inventory_interval_seconds',86400)))}
    started=time.monotonic(); last={k:started-v for k,v in intervals.items()}
    try:
        while not STOP:
            now=time.monotonic()
            if now-last['heartbeat']>=intervals['heartbeat']:
                queue.enqueue('heartbeat',{'agent_version':VERSION}); last['heartbeat']=now
            if now-last['health']>=intervals['health']:
                queue.enqueue('health',health_payload(collect_inventory(False))); last['health']=now
            if now-last['inventory']>=intervals['inventory']:
                inv=collect_inventory()
                if not config.get('supports_memory_total',False): inv.pop('memory_total_bytes',None)
                queue.enqueue('inventory',inv); last['inventory']=now
            flush(queue,config)
            if once: break
            time.sleep(5)
    finally: queue.close()


def configure_logging():
    LOG_FILE.parent.mkdir(parents=True,exist_ok=True)
    handler=RotatingFileHandler(LOG_FILE,maxBytes=2*1024*1024,backupCount=4,encoding='utf-8')
    logging.basicConfig(level=logging.INFO,handlers=[handler],format='%(asctime)s %(levelname)s %(message)s')


def _stop(*_):
    global STOP
    STOP=True


try:
    import win32event
    import win32service
    import win32serviceutil
    import servicemanager

    class IlonaAgentService(win32serviceutil.ServiceFramework):
        _svc_name_='IlonaAgent'
        _svc_display_name_='Ilona Agent'
        _svc_description_='Ilona Admin workstation inventory and health reporting agent.'
        def __init__(self,args):
            super().__init__(args)
            self.stop_event=win32event.CreateEvent(None,0,0,None)
        def SvcStop(self):
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            _stop()
            win32event.SetEvent(self.stop_event)
        def SvcDoRun(self):
            configure_logging()
            servicemanager.LogInfoMsg('Ilona Agent service started')
            try: run_agent(CONFIG)
            except Exception:
                logging.exception('Ilona Agent stopped after an error')
                raise
except ImportError:
    IlonaAgentService=None


def main(argv=None):
    parser=argparse.ArgumentParser(description='Ilona Windows workstation agent')
    parser.add_argument('--config',type=Path,default=CONFIG)
    parser.add_argument('--ca-file',type=Path,default=CA_FILE if CA_FILE.exists() else None)
    parser.add_argument('--log-level',default='INFO',choices=('DEBUG','INFO','WARNING','ERROR'))
    sub=parser.add_subparsers(dest='action',required=True)
    sub.add_parser('enroll'); sub.add_parser('once'); sub.add_parser('run')
    args=parser.parse_args(argv)
    logging.basicConfig(level=getattr(logging,args.log_level))
    if args.action=='enroll':
        enroll('https://ilona.ad.vapepa.info:10443',args.config,args.ca_file)
        return 0
    if args.action=='once':
        run_agent(args.config,once=True); return 0
    if args.action=='run':
        signal.signal(signal.SIGTERM,_stop); signal.signal(signal.SIGINT,_stop)
        run_agent(args.config); return 0
    return 2


if __name__=='__main__':
    if is_service_command(sys.argv[1:]):
        if IlonaAgentService is None: raise SystemExit('pywin32 puuttuu; asenna requirements-windows.txt.')
        win32serviceutil.HandleCommandLine(IlonaAgentService)
    else:
        raise SystemExit(main())
