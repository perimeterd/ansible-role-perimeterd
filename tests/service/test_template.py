from pathlib import Path
import unittest

from ansible.plugins.filter.core import to_nice_yaml
from jinja2 import Environment, StrictUndefined
import yaml


ROLE_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_PATH = ROLE_ROOT / "templates" / "perimeterd.yaml.j2"
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "minimal-config.yml"
MANAGED_HEADER = "# Managed by perimeterd.perimeterd. Do not edit."


def reverse_mappings(value):
    if isinstance(value, dict):
        return {key: reverse_mappings(item) for key, item in reversed(list(value.items()))}
    if isinstance(value, list):
        return [reverse_mappings(item) for item in value]
    return value


class ConfigurationTemplateTests(unittest.TestCase):
    def render(self, config):
        environment = Environment(undefined=StrictUndefined, keep_trailing_newline=True)
        environment.filters["to_nice_yaml"] = to_nice_yaml
        template = environment.from_string(TEMPLATE_PATH.read_text(encoding="utf-8"))
        return template.render(perimeterd_config=config)

    def test_serializes_only_the_complete_config_deterministically(self):
        config = yaml.safe_load(FIXTURE_PATH.read_text(encoding="utf-8"))

        rendered = self.render(config)
        reordered = self.render(reverse_mappings(config))

        self.assertEqual(rendered, reordered)
        header, separator, serialized = rendered.partition("\n")
        self.assertEqual(header, MANAGED_HEADER)
        self.assertTrue(separator)
        self.assertEqual(yaml.safe_load(serialized), config)
        self.assertEqual(rendered.count(MANAGED_HEADER), 1)
        self.assertTrue(rendered.endswith("\n"))


if __name__ == "__main__":
    unittest.main()
