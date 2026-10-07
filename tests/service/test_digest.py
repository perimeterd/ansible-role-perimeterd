import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml


ROLE = Path(__file__).resolve().parents[2]


class ConfigurationDigestTests(unittest.TestCase):
    def test_expected_digest_matches_exact_installed_template_bytes(self):
        # Run the production digest task through Ansible, not plain Jinja: its
        # escaping rules caused literal backslash-n bytes to be hashed before.
        digest_task = yaml.safe_load((ROLE / 'tasks/config.yml').read_text())[0]
        config = {
            'version': 1,
            'firewall': {'backend': 'nftables'},
            'policies': [],
            'ip_lists': {'unicode-feed': {'url': 'https://invalid.example/ä/feed'}},
        }
        with tempfile.TemporaryDirectory(prefix='pd-digest-') as temporary:
            root = Path(temporary)
            fixture = root / 'roles/digest_fixture'
            (fixture / 'tasks').mkdir(parents=True)
            (fixture / 'templates').symlink_to(ROLE / 'templates', target_is_directory=True)
            (fixture / 'tasks/main.yml').write_text(yaml.safe_dump([digest_task]))
            play = [{
                'name': 'Prove expected digest identifies installed configuration bytes',
                'hosts': 'localhost',
                'gather_facts': False,
                'vars': {'perimeterd_config': config},
                'tasks': [
                    {'name': 'Derive the production expected digest',
                     'ansible.builtin.include_role': {'name': 'digest_fixture'}},
                    {'name': 'Install the canonical configuration template',
                     'ansible.builtin.template': {
                         'src': str(ROLE / 'templates/perimeterd.yaml.j2'),
                         'dest': str(root / 'installed.yaml'), 'mode': '0600'}},
                    {'name': 'Hash actual installed bytes',
                     'ansible.builtin.stat': {'path': str(root / 'installed.yaml'),
                                             'checksum_algorithm': 'sha256'},
                     'register': 'installed'},
                    {'name': 'Require exact rendered-byte correlation',
                     'ansible.builtin.assert': {
                         'that': ['_perimeterd_config_sha256 == installed.stat.checksum']}},
                ],
            }]
            path = root / 'play.yml'
            path.write_text(yaml.safe_dump(play))
            environment = dict(os.environ, ANSIBLE_ROLES_PATH=str(root / 'roles'),
                               ANSIBLE_NOCOLOR='1')
            result = subprocess.run(
                ['ansible-playbook', '-i', 'localhost,', '-c', 'local', str(path)],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                env=environment, timeout=60, check=False)
            self.assertEqual(result.returncode, 0, result.stdout)


if __name__ == '__main__':
    unittest.main()
