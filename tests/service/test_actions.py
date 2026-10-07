import base64
from pathlib import Path
import unittest

from jinja2 import Environment, StrictUndefined
import yaml


TASKS = yaml.safe_load((Path(__file__).resolve().parents[2] / 'tasks/service.yml').read_text())
RECONCILE = next(task for task in TASKS if 'block' in task)
SELECT = next(task for task in RECONCILE['block'] if 'ansible.builtin.set_fact' in task)


class ServiceActionTests(unittest.TestCase):
    def setUp(self):
        self.environment = Environment(undefined=StrictUndefined)
        self.environment.filters['b64decode'] = lambda value: base64.b64decode(value).decode()
        self.values = {
            'perimeterd_service_state': 'started',
            '_perimeterd_service_active': True,
            '_perimeterd_package_changed': False,
            '_perimeterd_installed_executable': {'stat': {'dev': 1, 'inode': 2}},
            '_perimeterd_running_executable': {'stat': {'dev': 1, 'inode': 2}},
            '_perimeterd_expected_marker': 'config_sha256=abc\npackage=xyz\n',
            '_perimeterd_applied_marker': {'content': base64.b64encode(b'config_sha256=abc\npackage=xyz\n').decode()},
            '_perimeterd_config_sha256': 'a' * 64,
            'ansible_check_mode': False,
        }

    def action(self, **changes):
        values = dict(self.values, **changes)
        expression = SELECT['ansible.builtin.set_fact']['_perimeterd_service_action']
        return self.environment.from_string(expression).render(values)

    def enabled(self, task, **changes):
        values = dict(self.values, **changes)
        conditions = task.get('when', [])
        if isinstance(conditions, str):
            conditions = [conditions]
        return all(self.environment.compile_expression(condition)(**values) for condition in conditions)

    def test_inactive_starts_even_with_package_and_marker_changes(self):
        self.assertEqual(self.action(_perimeterd_service_active=False,
                                     _perimeterd_package_changed=True,
                                     _perimeterd_applied_marker={}), 'started')

    def test_package_change_and_stale_device_or_inode_restart_before_reload(self):
        self.assertEqual(self.action(_perimeterd_package_changed=True,
                                     _perimeterd_applied_marker={}), 'restarted')
        for stat in ({'dev': 3, 'inode': 2}, {'dev': 1, 'inode': 3}, {}):
            with self.subTest(stat=stat):
                self.assertEqual(self.action(_perimeterd_running_executable={'stat': stat},
                                             _perimeterd_applied_marker={}), 'restarted')

    def test_missing_or_changed_marker_reloads_identical_marker_does_not(self):
        self.assertEqual(self.action(), 'started')
        self.assertEqual(self.action(_perimeterd_applied_marker={}), 'reload')
        self.assertEqual(self.action(_perimeterd_applied_marker={'content': 'b2xk'}), 'reload')

    def test_stopped_does_not_enter_reconciliation(self):
        self.assertFalse(self.enabled(RECONCILE, perimeterd_service_state='stopped'))
        stopped = next(task for task in TASKS if task.get('ansible.builtin.systemd_service', {}).get('state') == 'stopped')
        self.assertTrue(self.enabled(stopped, perimeterd_service_state='stopped'))

    def test_reload_is_exclusive_and_check_mode_only_predicts(self):
        service = next(task for task in RECONCILE['block'] if 'ansible.builtin.systemd_service' in task)
        command = next(task for task in RECONCILE['block'] if 'ansible.builtin.command' in task)
        prediction = next(task for task in RECONCILE['block'] if 'ansible.builtin.debug' in task)
        for check in (False, True):
            values = {'_perimeterd_service_action': 'reload', 'ansible_check_mode': check}
            self.assertFalse(self.enabled(service, **values))
            self.assertEqual(self.enabled(command, **values), not check)
            self.assertEqual(self.enabled(prediction, **values), check)
        for action in ('started', 'restarted'):
            self.assertTrue(self.enabled(service, _perimeterd_service_action=action))
            self.assertFalse(self.enabled(command, _perimeterd_service_action=action))

    def test_checkpoint_tasks_never_write_in_check_mode(self):
        for module in ('ansible.builtin.file', 'ansible.builtin.copy'):
            task = next(task for task in RECONCILE['block'] if module in task)
            self.assertFalse(self.enabled(task, ansible_check_mode=True))
            self.assertTrue(self.enabled(task, ansible_check_mode=False))


if __name__ == '__main__':
    unittest.main()
