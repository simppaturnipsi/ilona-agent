import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1] / 'ilona_agent.py'
SPEC = importlib.util.spec_from_file_location('ilona_agent', MODULE)
agent = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(agent)


class AgentTests(unittest.TestCase):
    def test_agent_version_uses_year_month_revision_format(self):
        self.assertRegex(agent.VERSION, r'^\d{4}\.\d{1,2}\.\d+$')

    def test_os_release_parser_handles_quotes(self):
        with tempfile.NamedTemporaryFile('w', delete=False) as f:
            f.write('NAME="Ubuntu Linux"\nVERSION_ID="26.04"\nID=ubuntu\n')
            name = f.name
        try:
            parsed = agent.parse_os_release(name)
            self.assertEqual(parsed['NAME'], 'Ubuntu Linux')
            self.assertEqual(parsed['VERSION_ID'], '26.04')
        finally:
            os.unlink(name)

    def test_offline_queue_coalesces_heartbeats_and_keeps_reports(self):
        with tempfile.TemporaryDirectory() as temp:
            queue = agent.Queue(Path(temp) / 'queue.db')
            queue.enqueue('heartbeat', {'agent_version': 'one'})
            queue.enqueue('health', {'overall_state': 'OK'})
            queue.enqueue('heartbeat', {'agent_version': 'two'})
            pending = queue.pending()
            self.assertEqual(len(pending), 2)
            self.assertEqual([x[1] for x in pending], ['health', 'heartbeat'])
            queue.acknowledge(pending[0][0])
            self.assertEqual(len(queue.pending()), 1)
            queue.close()

    def test_unknown_battery_does_not_report_zero(self):
        payload = agent.health_payload({'batteries': [], 'filesystems': [], 'storage_devices': []})
        self.assertIsNone(payload['battery_health_percent'])
        self.assertIsNone(payload['measurements']['minimum_free_percent'])
        self.assertEqual(payload['overall_state'], 'OK')

    def test_https_config_is_required(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'config.json'
            path.write_text('{"server_url":"http://example","workstation_id":"00000000-0000-0000-0000-000000000001","agent_credential":"01234567890123456789012345678901"}')
            with self.assertRaises(ValueError):
                agent.load_config(path)


if __name__ == '__main__':
    unittest.main()
