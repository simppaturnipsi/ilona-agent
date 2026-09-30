import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1] / 'ilona_agent_windows.py'
SPEC = importlib.util.spec_from_file_location('ilona_agent_windows', MODULE)
agent = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(agent)


class WindowsAgentTests(unittest.TestCase):
    def test_pywin32_service_verb_can_follow_options(self):
        self.assertTrue(agent.is_service_command(['--startup', 'auto', 'install']))
        self.assertTrue(agent.is_service_command(['start']))
        self.assertFalse(agent.is_service_command(['--config', 'config.json', 'enroll']))

    def test_installer_uses_pywin32_option_order_and_reuses_enrollment(self):
        script = (MODULE.parent / 'install-windows.ps1').read_text(encoding='utf-8')
        self.assertIn("-not $SkipEnrollment -and -not (Test-Path $configPath)", script)
        self.assertIn("$python $agent --startup auto install", script)
        self.assertNotIn("$python $agent install --startup auto", script)

    def test_windows_11_pro_reports_edition_display_version_and_ubr(self):
        rows = [
            {'Manufacturer': 'Maker', 'Model': 'Model', 'Domain': 'EXAMPLE', 'PartOfDomain': True,
             'DNSHostName': 'WS-1'},
            {'Caption': 'Microsoft Windows 11 Pro', 'Version': '10.0.26100', 'BuildNumber': '26100',
             'OSArchitecture': '64-bit', 'InstallDate': '2026-02-01T00:00:00Z',
             'LastBootUpTime': '2026-09-28T12:00:00Z'},
            {'ProductName': 'Windows 10 Pro', 'EditionID': 'Professional', 'DisplayVersion': '24H2',
             'CurrentBuild': '26100', 'UBR': 4652},
            {'Manufacturer': 'BIOS Maker', 'SMBIOSBIOSVersion': 'F.10', 'SerialNumber': 'SERIAL',
             'ReleaseDate': '2025-01-02T00:00:00Z'},
        ]
        with patch.object(agent, 'one', side_effect=rows), patch.object(agent.time, 'time', return_value=1790596800):
            _, _, _, _, result = agent.device_data()
        self.assertIn('Windows 11 Pro', result['os']['product_name'])
        self.assertEqual(result['os']['version'], '24H2 (10.0.26100)')
        self.assertEqual(result['os']['build'], '26100.4652')
        self.assertEqual(result['serial_number'], 'SERIAL')

    def test_windows_11_education_is_not_collapsed_to_generic_windows(self):
        rows = [
            {'Manufacturer': 'Maker', 'Model': 'Model', 'Domain': 'WORKGROUP', 'PartOfDomain': False,
             'DNSHostName': 'WS-EDU'},
            {'Caption': 'Microsoft Windows 11 Education', 'Version': '10.0.22631', 'BuildNumber': '22631',
             'OSArchitecture': '64-bit'},
            {'ProductName': 'Windows 10 Education', 'EditionID': 'Education', 'DisplayVersion': '23H2',
             'CurrentBuild': '22631', 'UBR': 5472},
            {'Manufacturer': 'BIOS Maker', 'SMBIOSBIOSVersion': '1.0'},
        ]
        with patch.object(agent, 'one', side_effect=rows):
            _, _, _, _, result = agent.device_data()
        self.assertIn('Education', result['os']['product_name'])
        self.assertEqual(result['os']['version'], '23H2 (10.0.22631)')
        self.assertEqual(result['os']['build'], '22631.5472')

    def test_missing_storage_smart_and_battery_data_remain_unknown(self):
        with patch.object(agent, 'powershell_json', side_effect=[
                [{'Index': 0, 'Manufacturer': 'Maker', 'Model': 'Disk', 'SerialNumber': 'S1',
                  'InterfaceType': 'SCSI', 'MediaType': 'Fixed hard disk media', 'Size': 1024}], [], []]):
            disk = agent.storage_data()[0]
        self.assertIsNone(disk['smart_status'])
        self.assertIsNone(disk['temperature_c'])
        self.assertIsNone(disk['percentage_used'])
        with patch.object(agent, 'powershell_json', return_value=[]):
            self.assertEqual(agent.battery_data(), [])

    def test_database_queue_is_wal_and_keeps_reports_when_offline(self):
        with tempfile.TemporaryDirectory() as temp:
            queue = agent.Queue(Path(temp) / 'queue.db')
            try:
                queue.enqueue('heartbeat', {'agent_version': agent.VERSION})
                with patch.object(agent, 'api_request', side_effect=OSError('offline')):
                    self.assertFalse(agent.flush(queue, {'server_url': 'https://example',
                        'workstation_id': '00000000-0000-0000-0000-000000000001',
                        'agent_credential': 'x' * 32}))
                self.assertEqual(len(queue.pending()), 1)
                self.assertEqual(queue.db.execute('PRAGMA journal_mode').fetchone()[0].lower(), 'wal')
            finally:
                queue.close()


if __name__ == '__main__':
    unittest.main()
