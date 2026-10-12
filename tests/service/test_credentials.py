import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROLE = Path(__file__).resolve().parents[2]


class CredentialPathTests(unittest.TestCase):
    def test_only_managed_relative_paths_are_rejected(self):
        cases = (
            ('managed-relative', True, True, False),
            ('managed-absolute', True, True, True),
            ('caller-managed-relative', False, True, False),
            ('disabled-relative', True, False, False),
        )
        for name, managed, enabled, absolute in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory(prefix='pd-credential-path-') as temporary:
                root = Path(temporary)
                directory = root / 'credentials.d'
                directory.mkdir(mode=0o755)
                credential = directory / 'key'
                credential.write_text('existing-caller-key')
                credential.chmod(0o640)
                variables = {
                    'ansible_python_interpreter': '{{ ansible_playbook_python }}',
                    'perimeterd_config': {'crowdsec': {
                        'enabled': enabled,
                        'api_key_file': str(credential) if absolute else 'credentials.d/key',
                    }},
                }
                if managed:
                    variables['perimeterd_crowdsec_api_key'] = 'replacement-key'
                play = [{
                    'hosts': 'localhost',
                    'gather_facts': False,
                    'vars': variables,
                    'tasks': [{'ansible.builtin.include_tasks': str(ROLE / 'tasks/credentials.yml')}],
                }]
                path = root / 'play.yml'
                path.write_text(yaml.safe_dump(play))
                argv = ['ansible-playbook', '-i', 'localhost,', '-c', 'local', str(path)]
                if managed and enabled:
                    # Exercise validation and the production tasks without requiring
                    # root privileges to apply their root-owned file permissions.
                    argv.append('--check')
                environment = dict(os.environ, ANSIBLE_NOCOLOR='1')
                result = subprocess.run(argv, cwd=root, env=environment, text=True,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        timeout=60, check=False)
                expected_success = absolute or not managed or not enabled
                self.assertEqual(result.returncode == 0, expected_success, result.stdout)
                if not managed or not enabled:
                    self.assertEqual(credential.read_text(), 'existing-caller-key')
                    self.assertEqual(directory.stat().st_mode & 0o777, 0o755)
                    self.assertEqual(credential.stat().st_mode & 0o777, 0o640)


if __name__ == '__main__':
    unittest.main()
