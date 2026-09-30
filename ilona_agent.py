#!/usr/bin/python3
"""Headless Ubuntu agent for Ilona Admin workstation inventory."""
import argparse
import getpass
import json
import logging
import os
import platform
import re
import shutil
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

VERSION = '0.2.2'
DEFAULT_CONFIG = Path.home() / '.config/ilona-agent/config.json'
DEFAULT_CA = Path.home() / '.config/ilona-agent/server-ca.crt'
DEFAULT_STATE = Path.home() / '.local/state/ilona-agent/queue.db'
STOP = False


def command(args, timeout=15):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
        return result.stdout.strip() if result.returncode == 0 else ''
    except (OSError, subprocess.TimeoutExpired):
        return ''


def read_text(path):
    try:
        return Path(path).read_text(errors='replace').strip()
    except (OSError, UnicodeError):
        return None


def parse_os_release(path='/etc/os-release'):
    fields = {}
    for line in (read_text(path) or '').splitlines():
        if '=' in line:
            key, value = line.split('=', 1)
            fields[key] = value.strip().strip('"\'')
    return fields


def uptime_data():
    try:
        uptime = int(float(Path('/proc/uptime').read_text().split()[0]))
        return int(time.time()) - uptime, uptime
    except (OSError, ValueError, IndexError):
        return None, None


def dmi_value(name):
    return read_text('/sys/class/dmi/id/' + name)


def cpu_data():
    blocks = (read_text('/proc/cpuinfo') or '').split('\n\n')
    first = {}
    physical = set()
    for line in blocks[0].splitlines() if blocks else []:
        if ':' in line:
            key, value = line.split(':', 1)
            first[key.strip()] = value.strip()
    for block in blocks:
        values = dict(line.split(':', 1) for line in block.splitlines() if ':' in line)
        if values.get('physical id') is not None and values.get('core id') is not None:
            physical.add((values['physical id'].strip(), values['core id'].strip()))
    logical = len(re.findall(r'^processor\s*:', read_text('/proc/cpuinfo') or '', re.M)) or None
    cores = len(physical) or _int(first.get('cpu cores')) or logical
    return {'vendor': first.get('vendor_id') or first.get('CPU implementer'),
            'model': first.get('model name') or first.get('Model'),
            'physical_cores': cores, 'logical_processors': logical}


def _int(value):
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def memory_data():
    total = None
    for line in (read_text('/proc/meminfo') or '').splitlines():
        if line.startswith('MemTotal:'):
            fields = line.split()
            total = _int(fields[1]) * 1024 if len(fields) > 1 else None
            break
    # Module detail requires privileged SMBIOS access. Leave unavailable fields
    # absent instead of reporting invented zeroes.
    return total, []


def capacity_mwh(power_supply_path, key):
    value = _int(read_text(os.path.join(power_supply_path, key)))
    if value is None:
        return None
    # Linux energy_* sysfs values are µWh. Charge_* values need voltage to
    # convert and are therefore intentionally left unknown here.
    return value // 1000 if key.startswith('energy_') else None


def battery_data():
    batteries = []
    root = Path('/sys/class/power_supply')
    if not root.exists():
        return batteries
    for path in root.iterdir():
        if read_text(path / 'type') != 'Battery':
            continue
        design = capacity_mwh(path, 'energy_full_design')
        full = capacity_mwh(path, 'energy_full')
        current = capacity_mwh(path, 'energy_now')
        health = round(full / design * 100, 2) if full is not None and design else None
        status = read_text(path / 'status')
        batteries.append({'manufacturer': read_text(path / 'manufacturer'),
                          'model': read_text(path / 'model_name'),
                          'serial_number': read_text(path / 'serial_number'),
                          'design_capacity_mwh': design, 'full_charge_capacity_mwh': full,
                          'current_capacity_mwh': current,
                          'cycle_count': _int(read_text(path / 'cycle_count')),
                          'status': status, 'health_percent': health})
    return batteries


def storage_data():
    raw = command(['/usr/bin/lsblk', '--json', '--bytes', '-d', '-o',
                   'NAME,TYPE,SIZE,MODEL,SERIAL,TRAN,ROTA'], timeout=20)
    try:
        devices = json.loads(raw).get('blockdevices', [])
    except (ValueError, AttributeError):
        devices = []
    storage = []
    for device in devices:
        if device.get('type') != 'disk':
            continue
        transport = (device.get('tran') or '').lower()
        rota = device.get('rota')
        kind = 'NVME' if transport == 'nvme' or (device.get('name') or '').startswith('nvme') else (
            'HDD' if rota is True else ('SATA_SSD' if transport in ('sata', 'ata') else 'UNKNOWN'))
        item = {'id': device.get('name'), 'manufacturer': None,
                'model': device.get('model'), 'serial_number': device.get('serial'),
                'kind': kind, 'capacity_bytes': device.get('size'), 'health': {}}
        smartctl = shutil.which('smartctl')
        smart = command([smartctl, '-H', '-A', '-j', '/dev/' + device['name']], timeout=20) \
            if smartctl and device.get('name') else ''
        if smart:
            try:
                health = json.loads(smart)
                item['health'] = health
                item['smart_status'] = ('OK' if health.get('smart_status', {}).get('passed') else 'WARNING')
                item['temperature_c'] = health.get('temperature', {}).get('current')
                item['power_on_hours'] = health.get('power_on_time', {}).get('hours')
                nvme = health.get('nvme_smart_health_information_log', {})
                item['available_spare_percent'] = nvme.get('available_spare')
                item['percentage_used'] = nvme.get('percentage_used')
            except (ValueError, AttributeError):
                pass
        storage.append(item)
    return storage


def filesystem_data(storage):
    by_device = {}
    try:
        for line in Path('/proc/self/mounts').read_text(errors='replace').splitlines():
            fields = line.split()
            if len(fields) < 3:
                continue
            source, mount, fstype = fields[:3]
            mount = mount.replace('\\040', ' ')
            if fstype in ('proc', 'sysfs', 'devtmpfs', 'devpts', 'tmpfs', 'cgroup', 'cgroup2',
                          'squashfs', 'overlay', 'tracefs', 'securityfs', 'pstore', 'debugfs',
                          'configfs', 'fusectl', 'mqueue', 'hugetlbfs', 'rpc_pipefs'):
                continue
            try:
                values = os.statvfs(mount)
                total = values.f_blocks * values.f_frsize
                free = values.f_bavail * values.f_frsize
                if total <= 0:
                    continue
                source_name = os.path.basename(os.path.realpath(source))
                storage_id = next((d.get('id') for d in storage if d.get('id') and
                                   source_name.startswith(d['id'])), None)
                by_device[(mount, fstype)] = {'storage_id': storage_id, 'mount_point': mount,
                    'filesystem': fstype, 'capacity_bytes': total, 'used_bytes': total - values.f_bfree * values.f_frsize,
                    'free_bytes': free, 'free_percent': round(free / total * 100, 2)}
            except OSError:
                continue
    except OSError:
        pass
    return list(by_device.values())


def network_data():
    result = command(['/usr/sbin/ip', '-j', 'address', 'show']) or command(['/sbin/ip', '-j', 'address', 'show'])
    try:
        interfaces = json.loads(result)
    except (ValueError, TypeError):
        interfaces = []
    output = []
    for iface in interfaces:
        name = iface.get('ifname', '')
        addresses = iface.get('addr_info', [])
        ipv4 = next((x.get('local') for x in addresses if x.get('family') == 'inet'), None)
        ipv6 = next((x.get('local') for x in addresses if x.get('family') == 'inet6' and x.get('scope') != 'link'), None)
        raw_kind = read_text(f'/sys/class/net/{name}/type')
        kind = {'1': 'ethernet', '772': 'loopback', '512': 'ppp', '768': 'ipip'}.get(raw_kind, raw_kind)
        output.append({'name': name, 'interface_type': 'wireguard' if name.startswith(('wg', 'ilona')) else kind,
                       'mac_address': iface.get('address'), 'ipv4': ipv4, 'ipv6': ipv6,
                       'link_state': iface.get('operstate'), 'is_vpn': int(name.startswith(('wg', 'ilona')))})
    return output


def domain_data():
    realms = command(['/usr/sbin/realm', 'list']) or command(['/usr/bin/realm', 'list'])
    if not realms:
        return {'joined': 0, 'domain_name': None, 'computer_account': None, 'connection_state': 'NOT_JOINED'}
    domain = None
    for line in realms.splitlines():
        if line.strip() and ':' not in line:
            domain = line.strip()
            break
    state = None
    sss = command(['/usr/bin/sssctl', 'domain-status', domain]) if domain else ''
    if sss:
        state = 'ONLINE' if re.search(r'Online status:\s+Online', sss, re.I) else 'OFFLINE'
    return {'joined': 1, 'domain_name': domain, 'computer_account': read_text('/etc/hostname'),
            'connection_state': state}


def software_data():
    raw = command(['/usr/bin/dpkg-query', '-W', '-f=${binary:Package}\t${Version}\t${Maintainer}\n'], timeout=60)
    rows = []
    for line in raw.splitlines():
        fields = line.split('\t', 2)
        if fields and fields[0]:
            rows.append({'source': 'dpkg', 'name': fields[0],
                         'version': fields[1] if len(fields) > 1 else None,
                         'publisher': fields[2] if len(fields) > 2 else None,
                         'install_date': None})
    return rows[:5000]


def collect_inventory(include_software=True):
    os_info = parse_os_release()
    booted_at, uptime = uptime_data()
    storage = storage_data()
    memory_total, modules = memory_data()
    inventory = {
        'agent_version': VERSION, 'hostname': socket.gethostname(),
        'manufacturer': dmi_value('sys_vendor'), 'model': dmi_value('product_name'),
        'serial_number': dmi_value('product_serial'), 'asset_tag': dmi_value('chassis_asset_tag'),
        'architecture': platform.machine(),
        'os': {'family': 'Linux', 'product_name': os_info.get('PRETTY_NAME') or os_info.get('NAME'),
               'version': os_info.get('VERSION_ID'), 'build': None, 'kernel': platform.release(),
               'architecture': platform.machine(), 'install_date': None,
               'booted_at': booted_at, 'uptime_seconds': uptime},
        'cpu': cpu_data(), 'bios': {'manufacturer': dmi_value('bios_vendor'),
            'version': dmi_value('bios_version'), 'release_date': dmi_value('bios_date')},
        'domain': domain_data(), 'memory_modules': modules,
        'storage_devices': storage, 'filesystems': filesystem_data(storage),
        'batteries': battery_data(), 'network_interfaces': network_data(),
        'installed_software': software_data() if include_software else [],
    }
    inventory['memory_total_bytes'] = memory_total
    return inventory


def health_payload(inventory=None):
    inventory = inventory or collect_inventory()
    batteries = [b['health_percent'] for b in inventory.get('batteries', []) if b.get('health_percent') is not None]
    fs = [f['free_percent'] for f in inventory.get('filesystems', []) if f.get('free_percent') is not None]
    disks = inventory.get('storage_devices', [])
    smart = [d.get('smart_status') for d in disks if d.get('smart_status')]
    wear = [d.get('percentage_used') for d in disks if isinstance(d.get('percentage_used'), (int, float))]
    state = 'WARNING' if any(s in ('WARNING', 'FAILED') for s in smart) or (fs and min(fs) < 10) else 'OK'
    if batteries and min(batteries) < 70:
        state = 'WARNING'
    return {'battery_health_percent': min(batteries) if batteries else None,
            'overall_state': state,
            'measurements': {'smart_status': ('FAILED' if 'FAILED' in smart else ('WARNING' if 'WARNING' in smart else ('OK' if 'OK' in smart else None))),
                             'minimum_free_percent': min(fs) if fs else None,
                             'maximum_percentage_used': max(wear) if wear else None}}


class Queue:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        self.db = sqlite3.connect(self.path, timeout=10)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA busy_timeout=10000')
        self.db.execute('CREATE TABLE IF NOT EXISTS reports(id INTEGER PRIMARY KEY,kind TEXT NOT NULL,payload TEXT NOT NULL,created_at INTEGER NOT NULL)')
        self.db.execute('CREATE INDEX IF NOT EXISTS ix_reports_created ON reports(id)')
        self.db.commit()
        self.path.chmod(0o600)

    def enqueue(self, kind, payload):
        with self.db:
            if kind == 'heartbeat':
                self.db.execute('DELETE FROM reports WHERE kind=?', (kind,))
            self.db.execute('INSERT INTO reports(kind,payload,created_at) VALUES(?,?,?)',
                            (kind, json.dumps(payload, separators=(',', ':')), int(time.time())))
            self.db.execute('DELETE FROM reports WHERE id NOT IN (SELECT id FROM reports ORDER BY id DESC LIMIT 10000)')

    def pending(self):
        return self.db.execute('SELECT id,kind,payload FROM reports ORDER BY id LIMIT 100').fetchall()

    def acknowledge(self, item_id):
        with self.db:
            self.db.execute('DELETE FROM reports WHERE id=?', (item_id,))

    def close(self):
        self.db.close()


def api_request(url, path, payload=None, credential=None, timeout=15, ca_file=None, method='POST'):
    data = json.dumps(payload, separators=(',', ':')).encode() if payload is not None else None
    headers = {'User-Agent': f'Ilona-Agent/{VERSION}'}
    if data is not None:
        headers['Content-Type'] = 'application/json'
    if credential:
        headers['Authorization'] = 'Bearer ' + credential
    req = urllib.request.Request(url.rstrip('/') + path, data=data, headers=headers, method=method)
    context = ssl.create_default_context(cafile=str(ca_file)) if ca_file else ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout, context=context) as response:
        raw = response.read(1024 * 1024)
        return response.status, json.loads(raw) if raw else {}


def enroll(url, config_path, ca_file=None):
    token = getpass.getpass('Kertakäyttöinen Ilona enrollment-token: ').strip()
    if not token:
        raise SystemExit('Enrollment-token puuttuu.')
    supports_memory_total = False
    try:
        _, caps = api_request(url, '/api/v1/workstations/capabilities', ca_file=ca_file, method='GET')
        supports_memory_total = 'memory_total_bytes' in caps.get('inventory_fields', [])
    except urllib.error.HTTPError:
        pass  # Older API instances safely receive only their supported fields.
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SystemExit(f'Ilona Adminiin ei saatu HTTPS-yhteyttä ({type(exc).__name__}).') from None
    try:
        status, result = api_request(url, '/api/v1/workstations/enroll',
                                     {'token': token, 'agent_version': VERSION}, ca_file=ca_file)
    except urllib.error.HTTPError as exc:
        raise SystemExit(f'Enrollment epäonnistui (HTTP {exc.code}).') from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SystemExit(f'Ilona Adminiin ei saatu HTTPS-yhteyttä ({type(exc).__name__}).') from None
    if status != 201 or not result.get('workstation_id') or not result.get('agent_credential'):
        raise SystemExit(f'Enrollment epäonnistui (HTTP {status}).')
    config = {'server_url': url.rstrip('/'), 'workstation_id': result['workstation_id'],
              'agent_credential': result['agent_credential'], 'heartbeat_interval_seconds': 60,
              'health_interval_seconds': 3600, 'inventory_interval_seconds': 86400,
              'state_database': str(DEFAULT_STATE), 'supports_memory_total': supports_memory_total}
    if ca_file:
        config['ca_bundle'] = str(ca_file)
    config_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix='.config-', dir=config_path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(config, stream, indent=2)
            stream.write('\n')
        os.replace(tmp_name, config_path)
        config_path.chmod(0o600)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    print(f'Enrollment valmis: {result["workstation_id"]}')
    print(f'Tunnistetiedosto tallennettu oikeuksin 0600: {config_path}')


def load_config(path):
    config = json.loads(Path(path).read_text())
    required = ('server_url', 'workstation_id', 'agent_credential')
    if any(not isinstance(config.get(k), str) or not config[k] for k in required):
        raise ValueError('Agent configuration is incomplete')
    config['workstation_id'] = str(uuid.UUID(config['workstation_id']))
    if not config['server_url'].startswith('https://'):
        raise ValueError('API URL must use HTTPS')
    if len(config['agent_credential']) < 32:
        raise ValueError('Agent credential is invalid')
    return config


def flush(queue, config):
    errors = 0
    for row_id, kind, payload_text in queue.pending():
        path = f"/api/v1/workstations/{config['workstation_id']}/{kind}"
        try:
            api_request(config['server_url'], path, json.loads(payload_text), config['agent_credential'],
                        ca_file=config.get('ca_bundle'))
            queue.acknowledge(row_id)
            logging.info('Delivered %s report', kind)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            status = getattr(exc, 'code', None)
            logging.warning('Ilona API report not accepted; report remains queued (type=%s status=%s)',
                            type(exc).__name__, status or 'unavailable')
            errors += 1
            break
    return errors == 0


def run_agent(config_path, state_path=None, once=False):
    config = load_config(config_path)
    queue = Queue(state_path or config.get('state_database') or DEFAULT_STATE)
    intervals = {'heartbeat': max(30, int(config.get('heartbeat_interval_seconds', 60))),
                 'health': max(300, int(config.get('health_interval_seconds', 3600))),
                 'inventory': max(3600, int(config.get('inventory_interval_seconds', 86400)))}
    started = time.monotonic()
    last = {kind: started - interval for kind, interval in intervals.items()}
    try:
        while not STOP:
            now = time.monotonic()
            if now - last['heartbeat'] >= intervals['heartbeat']:
                queue.enqueue('heartbeat', {'agent_version': VERSION})
                last['heartbeat'] = now
            if now - last['health'] >= intervals['health']:
                inv = collect_inventory(include_software=False)
                queue.enqueue('health', health_payload(inv))
                last['health'] = now
            if now - last['inventory'] >= intervals['inventory']:
                inventory = collect_inventory()
                if not config.get('supports_memory_total', False):
                    inventory.pop('memory_total_bytes', None)
                queue.enqueue('inventory', inventory)
                last['inventory'] = now
            flush(queue, config)
            if once:
                break
            time.sleep(5)
    finally:
        queue.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Ilona Ubuntu workstation agent')
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--ca-file', type=Path, default=DEFAULT_CA if DEFAULT_CA.exists() else None,
                        help='trusted Ilona Admin self-signed certificate')
    parser.add_argument('--state-database', type=Path)
    parser.add_argument('--log-level', default='INFO', choices=('DEBUG', 'INFO', 'WARNING', 'ERROR'))
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('enroll', help='Enroll this machine using a one-time token')
    once = sub.add_parser('once', help='Collect and send any due reports once')
    sub.add_parser('run', help='Run the background reporting loop')
    args = parser.parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level), format='%(asctime)s %(levelname)s %(message)s')
    if args.action == 'enroll':
        enroll('https://ilona.ad.vapepa.info:10443', args.config, args.ca_file)
        try:
            subprocess.run(['/usr/bin/systemctl', '--user', 'enable', '--now', 'ilona-agent.service'],
                           check=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            print('Agentti rekisteröity. Käynnistä palvelu komennolla: systemctl --user enable --now ilona-agent')
        return 0
    signal.signal(signal.SIGTERM, lambda *_: _stop())
    signal.signal(signal.SIGINT, lambda *_: _stop())
    run_agent(args.config, args.state_database, once=args.action == 'once')
    return 0


def _stop():
    global STOP
    STOP = True


if __name__ == '__main__':
    sys.exit(main())
