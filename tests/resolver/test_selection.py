"""Exercise the real resolver tasks through an isolated TLS GitHub API fixture.

The proxy preserves the role's fixed https://api.github.com URLs: test code
intercepts the controller connection, not a production-configurable API origin.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import ssl
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from vm import run as harness

ROLE = Path(__file__).resolve().parents[2]
API = "/repos/perimeterd/perimeterd/releases"
SECRET = "fixture-not-a-real-token-428571"


def release(tag, *, published="2026-09-26T12:00:00Z", identifier=12,
            prerelease=False, draft=False, packages=True):
    version = tag.removeprefix("v")
    names = ["checksums.txt"]
    if packages:
        names += [f"perimeterd_{version}-1_amd64.deb",
                  f"perimeterd-{version}-1.x86_64.rpm"]
    assets = []
    for number, name in enumerate(names, start=1):
        assets.append({"id": identifier * 10 + number, "name": name,
                       "state": "uploaded", "size": 42,
                       "url": f"https://api.github.com{API}/assets/{identifier * 10 + number}",
                       "browser_download_url":
                       f"https://github.com/perimeterd/perimeterd/releases/download/{tag}/{name}"})
    return {"id": identifier, "tag_name": tag, "published_at": published,
            "created_at": "2026-09-25T12:00:00Z", "draft": draft,
            "prerelease": prerelease, "assets": assets}


def arm_release(tag, **kwargs):
    """Keep ARM requirements confined to the shared-Fedora resolver coverage."""
    payload = release(tag, **kwargs)
    if kwargs.get("packages", True):
        package = dict(payload["assets"][-1])
        package["name"] = f"perimeterd-{tag.removeprefix('v')}-1.aarch64.rpm"
        package["browser_download_url"] = (
            f"https://github.com/perimeterd/perimeterd/releases/download/{tag}/{package['name']}")
        payload["assets"] = [payload["assets"][0], package]
    return payload


class FixtureProxy(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def do_CONNECT(self):
        if self.path != f"{getattr(self.server, 'allowed_host', 'api.github.com')}:443":
            self.send_error(403, "unexpected proxy destination")
            return
        self.send_response(200, "Connection Established")
        self.end_headers()
        self.connection = self.server.tls_context.wrap_socket(self.connection, server_side=True)
        self.rfile = self.connection.makefile("rb", self.rbufsize)
        self.wfile = self.connection.makefile("wb", self.wbufsize)
        self.close_connection = True
        try:
            self.handle_one_request()
        finally:
            self.connection.close()

    def do_GET(self):
        self.server.calls.append((self.path, self.headers.get("Authorization"),
                                  self.headers.get("X-GitHub-Api-Version")))
        route = self.server.routes.get(
            self.path, (404, {"message": "fixture path not configured"}, {}))
        if isinstance(route, list):
            route = route.pop(0)
        status, payload, extra = route
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        for header, value in extra.items():
            self.send_header(header, value)
        self.end_headers()
        self.wfile.write(body)


class ResolverWithAnsible(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="perimeterd-api-fixture-")
        cls.work = Path(cls.temporary.name)
        key, cert = cls.work / "tls.key", cls.work / "tls.crt"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-days", "1", "-subj", "/CN=api.github.com",
                        "-addext", "subjectAltName=DNS:api.github.com",
                        "-keyout", str(key), "-out", str(cert)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureProxy)
        cls.server.routes = {}
        cls.server.calls = []
        cls.server.tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        cls.server.tls_context.load_cert_chain(certfile=cert, keyfile=key)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.cert = cert
        cls.playbook = cls.work / "resolve.yml"
        cls.playbook.write_text(
            "---\n- hosts: fixture\n  gather_facts: false\n  connection: local\n"
            "  vars:\n    ansible_python_interpreter: '{{ ansible_playbook_python }}'\n"
            f"    perimeterd_github_token: {SECRET!r}\n"
            "    perimeterd_download_timeout: 10\n"
            "  tasks:\n"
            "    - name: Select package family for this fixture host\n"
            "      ansible.builtin.set_fact:\n"
            "        _perimeterd_package_format: deb\n"
            "        _perimeterd_architecture: amd64\n"
            "    - name: Execute the real release resolver task file\n"
            "      ansible.builtin.include_role:\n"
            f"        name: {str(ROLE)!r}\n"
            "        tasks_from: resolve\n"
            "    - name: Expose only the selected public identity\n"
            "      ansible.builtin.debug:\n"
            "        msg: 'RESOLVED_TAG={{ _perimeterd_selected_tag }} ASSET={{ _perimeterd_asset.name }}'\n")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=10)
        cls.temporary.cleanup()

    def setUp(self):
        self.server.routes = {}
        self.server.calls = []

    def fixture_environment(self):
        return dict(os.environ, HTTPS_PROXY=f"http://127.0.0.1:{self.server.server_port}",
                    https_proxy=f"http://127.0.0.1:{self.server.server_port}",
                    HTTP_PROXY="", http_proxy="", ALL_PROXY="", all_proxy="",
                    NO_PROXY="", no_proxy="", SSL_CERT_FILE=str(self.cert),
                    ANSIBLE_NOCOLOR="1", PERIMETERD_TEST_GITHUB_TOKEN=SECRET)

    def invoke(self, selector="latest", *, success=True):
        env = self.fixture_environment()
        result = subprocess.run(
            ["ansible-playbook", "-i", "fixture,", str(self.playbook), "-e",
             json.dumps({"perimeterd_version": selector})],
            cwd=ROLE, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, timeout=120)
        self.assertNotIn(SECRET, result.stdout)
        if success and result.returncode != 0:
            self.fail(f"Ansible resolver unexpectedly failed:\n{result.stdout}")
        if not success and result.returncode == 0:
            self.fail(f"Ansible resolver unexpectedly accepted fixture:\n{result.stdout}")
        self.assertTrue(self.server.calls, "actual controller API request did not reach fixture")
        for _path, token, api_version in self.server.calls:
            self.assertEqual(token, f"Bearer {SECRET}")
            self.assertEqual(api_version, "2026-03-10")
        return result.stdout

    def invoke_release_playbook(self, work, *, output=True, success=True):
        roles = work / "roles"
        roles.mkdir(exist_ok=True)
        (roles / "perimeterd").symlink_to(ROLE, target_is_directory=True)
        destination = work / "release.json"
        variables = {"perimeterd_download_timeout": 10}
        if output is not False:
            variables["vm_release_output"] = str(destination) if output is True else output
        result = subprocess.run(
            ["ansible-playbook", "-i", "localhost,", str(ROLE / "tests/vm/resolve-release.yml"),
             "-e", json.dumps(variables)],
            cwd=ROLE, env=dict(self.fixture_environment(), ANSIBLE_ROLES_PATH=str(roles)),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=120)
        self.assertNotIn(SECRET, result.stdout)
        self.assertEqual(result.returncode == 0, success, result.stdout)
        for _path, token, api_version in self.server.calls:
            self.assertEqual(token, f"Bearer {SECRET}")
            self.assertEqual(api_version, "2026-03-10")
        return destination

    def test_fedora_release_playbook_writes_only_selected_arm_tag(self):
        tag = "v1.2.3-dev.7+build.4"
        self.server.routes[API + "?per_page=100"] = (
            200, [arm_release(tag, prerelease=True)], {})
        with tempfile.TemporaryDirectory() as temporary:
            destination = self.invoke_release_playbook(Path(temporary))
            self.assertEqual(json.loads(destination.read_text()), {"tag": tag})
        self.assertEqual([path for path, *_ in self.server.calls], [API + "?per_page=100"])

    def test_fedora_release_playbook_failure_has_no_tag_output(self):
        tag = "1.2.3-dev.7"
        missing_checksum = arm_release(tag, prerelease=True)
        missing_checksum["assets"] = missing_checksum["assets"][1:]
        cases = {
            "missing ARM package": (200, [release(tag, prerelease=True)], {}),
            "missing checksum": (200, [missing_checksum], {}),
            "API failure": (401, {"message": "Bad credentials"}, {}),
        }
        for name, response in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                self.server.calls = []
                self.server.routes[API + "?per_page=100"] = response
                destination = self.invoke_release_playbook(Path(temporary), success=False)
                self.assertFalse(destination.exists())
                self.assertEqual([path for path, *_ in self.server.calls], [API + "?per_page=100"])

    def test_fedora_release_playbook_requires_output_before_api(self):
        for output in (False, "", "   ", 42):
            with self.subTest(output=output), tempfile.TemporaryDirectory() as temporary:
                destination = self.invoke_release_playbook(
                    Path(temporary), output=output, success=False)
                self.assertFalse(destination.exists())
            self.assertEqual(self.server.calls, [])

    def test_shared_fedora_selection_pins_real_guest_resolver_after_channel_moves(self):
        tag = "v1.2.3-dev.7+build.4"
        list_route = API + "?per_page=100"
        exact_route = API + "/tags/v1.2.3-dev.7%2Bbuild.4"
        self.server.routes[list_route] = (200, [arm_release(tag, prerelease=True)], {})
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            destination = self.invoke_release_playbook(work)
            selected = json.loads(destination.read_text())["tag"]
            self.server.routes[list_route] = (
                200, [arm_release("v1.2.3-dev.8+build.5", identifier=99, prerelease=True)], {})
            self.server.routes[exact_route] = (200, arm_release(tag, prerelease=True), {})
            playbook = work / "pinned-resolver.yml"
            playbook.write_text(
                "---\n- hosts: target\n  connection: local\n  gather_facts: false\n"
                "  become: false\n  vars:\n"
                "    ansible_python_interpreter: '{{ ansible_playbook_python }}'\n"
                "    perimeterd_version: '{{ smoke_version }}'\n"
                "    perimeterd_github_token: \"{{ lookup('ansible.builtin.env', 'PERIMETERD_TEST_GITHUB_TOKEN') }}\"\n"
                "    perimeterd_download_timeout: 10\n"
                "    _perimeterd_package_format: rpm\n    _perimeterd_architecture: arm64\n"
                "  tasks:\n    - ansible.builtin.include_role:\n"
                "        name: perimeterd\n        tasks_from: resolve\n"
                "    - ansible.builtin.debug:\n"
                "        msg: 'RESOLVED_TAG={{ _perimeterd_selected_tag }}'\n")
            logs = work / "logs"
            logs.mkdir()
            timings = harness.Timings(logs, "fedora-arm64", "published", "tcg", selected)
            guest = harness.Guest(1, work / "unused-key", logs, "fedora-arm64",
                                  timings=timings, scenario="current-format", release_tag=selected)
            fixture_env = self.fixture_environment()
            guest.controller_environment = {
                name: fixture_env[name] for name in (
                    "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
                    "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy",
                    "SSL_CERT_FILE", "PERIMETERD_TEST_GITHUB_TOKEN")}
            real_run = subprocess.run

            def local_resolver(argv, **kwargs):
                # Substitute transport/playbook only; Guest.play supplies the real baseline variables.
                argv = list(argv)
                self.assertEqual(argv[0], "ansible-playbook")
                self.assertEqual(Path(argv[3]).parent, ROLE / "tests/vm")
                argv[2:4] = ["target,", str(playbook)]
                return real_run(argv, **kwargs)

            with patch.object(harness.subprocess, "run", side_effect=local_resolver):
                for label in ("initial", "repeat"):
                    self.assertIn(f"RESOLVED_TAG={tag}", guest.play(label))
            self.assertEqual(guest.version, tag)
            self.assertEqual(timings.version, tag)
            records = [json.loads(line) for line in timings.path.read_text().splitlines()]
            self.assertEqual([record["version"] for record in records], [tag, tag])
            self.assertEqual([record["scenario"] for record in records],
                             ["current-format", "current-format"])
            self.assertEqual([record["outcome"] for record in records], ["passed", "passed"])
        self.assertEqual([path for path, *_ in self.server.calls],
                         [list_route, exact_route, exact_route])
        for _path, token, api_version in self.server.calls:
            self.assertEqual(token, f"Bearer {SECRET}")
            self.assertEqual(api_version, "2026-03-10")

    def test_latest_uses_github_designated_stable_without_listing_prereleases(self):
        self.server.routes[API + "/latest"] = (200, release("1.2.3"), {})
        output = self.invoke()
        self.assertIn("RESOLVED_TAG=1.2.3 ASSET=perimeterd_1.2.3-1_amd64.deb", output)
        self.assertEqual([path for path, *_ in self.server.calls], [API + "/latest"])

    def test_exact_prerelease_never_resolves_latest(self):
        tag = "0.0.1-dev.7.g607202599b29"
        self.server.routes[f"{API}/tags/{tag}"] = (200, release(tag, prerelease=True), {})
        self.assertIn(f"RESOLVED_TAG={tag}", self.invoke(tag))
        self.assertEqual([path for path, *_ in self.server.calls], [f"{API}/tags/{tag}"])

    def test_exact_v_prefix_is_not_silently_stripped_from_lookup(self):
        self.server.routes[API + "/tags/v1.2.3"] = (200, release("v1.2.3"), {})
        self.assertIn("ASSET=perimeterd_1.2.3-1_amd64.deb", self.invoke("v1.2.3"))

    def test_exact_semver_build_metadata_preserves_safe_asset_name(self):
        tag = "v1.2.3+build.4"
        route = API + "/tags/v1.2.3%2Bbuild.4"
        self.server.routes[route] = (200, release(tag), {})
        self.assertIn("ASSET=perimeterd_1.2.3+build.4-1_amd64.deb", self.invoke(tag))
        self.assertEqual([path for path, *_ in self.server.calls], [route])

    def test_exact_stable_tag_does_not_follow_a_newer_channel(self):
        self.server.routes[API + "/tags/1.2.3"] = (200, release("1.2.3"), {})
        self.assertIn("RESOLVED_TAG=1.2.3", self.invoke("1.2.3"))
        self.assertEqual([path for path, *_ in self.server.calls], [API + "/tags/1.2.3"])

    def test_missing_stable_has_no_prerelease_fallback(self):
        self.server.routes[API + "/latest"] = (404, {"message": "Not Found"}, {})
        output = self.invoke(success=False)
        self.assertIn("No published stable perimeterd release", output)
        self.assertEqual([path for path, *_ in self.server.calls], [API + "/latest"])

    def test_prerelease_selection_is_bounded_and_uses_publication_then_numeric_id(self):
        early = release("0.0.1-dev.6.g123456789abc", identifier=10, prerelease=True)
        newer = release("0.0.1-dev.7.g607202599b29", identifier=30, prerelease=True,
                        published="2026-09-27T00:00:00Z")
        tie = release("0.0.1-dev.8.gabc123", identifier=31, prerelease=True,
                      published="2026-09-27T00:00:00Z")
        list_url = API + "?per_page=100"
        next_url = list_url + "&page=2"
        first_page = [
            early, tie, newer,
            release("1.2.3", identifier=40, published="2026-09-29T00:00:00Z"),
            release("0.0.1-dev.10.gabc123", identifier=50,
                    published="2026-09-28T00:00:00Z", prerelease=True, draft=True),
        ] + [release(f"1.0.{n}", identifier=100 + n) for n in range(95)]
        self.server.routes[list_url] = (
            200, first_page,
            {"Link": f'<https://api.github.com{next_url}>; rel="next"'})
        self.server.routes[next_url] = (
            200, [release("0.0.1-dev.11.gabc123", identifier=60, prerelease=True,
                          published="2026-09-30T00:00:00Z")], {})
        self.assertIn("RESOLVED_TAG=0.0.1-dev.8.gabc123", self.invoke("latest-prerelease"))
        self.assertEqual([path for path, *_ in self.server.calls], [list_url])

    def test_link_headers_do_not_affect_single_request_selection(self):
        list_url = API + "?per_page=100"
        self.server.routes[list_url] = (
            200, [release("0.0.1-dev.7.g607202599b29", prerelease=True)],
            {"Link": (f'<https://evil.invalid/releases?page=2>; rel="next", '
                      f'<https://api.github.com{list_url}&page=3>; rel="next"')})
        self.assertIn("RESOLVED_TAG=0.0.1-dev.7.g607202599b29",
                      self.invoke("latest-prerelease"))
        self.assertEqual([path for path, *_ in self.server.calls], [list_url])

    def test_missing_package_asset_is_not_an_architecture_fallback(self):
        self.server.routes[API + "/tags/1.2.3"] = (200, release("1.2.3", packages=False), {})
        self.assertIn("package", self.invoke("1.2.3", success=False).lower())

    def test_missing_prerelease_in_window_has_no_next_page_or_stable_fallback(self):
        list_url = API + "?per_page=100"
        next_url = list_url + "&page=2"
        self.server.routes[list_url] = (
            200, [release(f"1.0.{n}", identifier=100 + n) for n in range(100)],
            {"Link": f'<https://api.github.com{next_url}>; rel="next"'})
        self.server.routes[next_url] = (
            200, [release("0.0.1-dev.7.g607202599b29", prerelease=True)], {})
        self.invoke("latest-prerelease", success=False)
        self.assertEqual([path for path, *_ in self.server.calls], [list_url])

    def test_ambiguous_package_and_checksum_assets_are_rejected(self):
        with self.subTest("duplicate package"):
            ambiguous = release("1.2.3")
            ambiguous["assets"].append(dict(ambiguous["assets"][1], id=999))
            self.server.routes[API + "/latest"] = (200, ambiguous, {})
            self.assertIn("exactly one package", self.invoke(success=False))
        with self.subTest("duplicate manifest"):
            ambiguous = release("1.2.3")
            ambiguous["assets"].append(dict(ambiguous["assets"][0], id=998))
            self.server.routes[API + "/latest"] = (200, ambiguous, {})
            self.assertIn("exactly one checksums.txt", self.invoke(success=False))

    def assert_tracking(self, selector, route, releases, tags):
        self.server.routes[route] = [(200, payload, {}) for payload in releases]
        playbook = self.work / "tracking.yml"
        playbook.write_text(
            "---\n- hosts: fixture\n  gather_facts: false\n  connection: local\n"
            "  vars:\n    ansible_python_interpreter: '{{ ansible_playbook_python }}'\n"
            "    perimeterd_download_timeout: 10\n    perimeterd_github_token: ''\n"
            f"    perimeterd_version: {selector}\n"
            "    _perimeterd_package_format: deb\n    _perimeterd_architecture: amd64\n"
            "  tasks:\n"
            + "".join(
                "    - ansible.builtin.include_role:\n"
                f"        name: {str(ROLE)!r}\n        tasks_from: resolve\n"
                "    - ansible.builtin.assert:\n"
                f"        that: _perimeterd_selected_tag == {tag!r}\n"
                for tag in tags)
        )
        env = dict(os.environ, HTTPS_PROXY=f"http://127.0.0.1:{self.server.server_port}",
                   https_proxy=f"http://127.0.0.1:{self.server.server_port}",
                   HTTP_PROXY="", http_proxy="", ALL_PROXY="", all_proxy="",
                   NO_PROXY="", no_proxy="", SSL_CERT_FILE=str(self.cert),
                   ANSIBLE_NOCOLOR="1")
        result = subprocess.run(["ansible-playbook", "-i", "fixture,", str(playbook)],
                                cwd=ROLE, env=env, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual([path for path, *_ in self.server.calls], [route, route])

    def test_floating_release_re_resolves_within_one_play(self):
        self.assert_tracking("latest", API + "/latest",
                             [release("1.2.3", identifier=20),
                              release("1.2.4", identifier=21)],
                             ("1.2.3", "1.2.4"))

    def test_prerelease_tracking_re_resolves_within_one_play(self):
        tags = ("0.0.1-dev.6.g123456789abc", "0.0.1-dev.7.g607202599b29")
        self.assert_tracking(
            "latest-prerelease", API + "?per_page=100",
            [[release(tag, identifier=index, prerelease=True)]
             for index, tag in enumerate(tags, start=20)],
            tags)

    def test_selector_changes_within_one_play_do_not_reuse_release_state(self):
        steps = [
            ("latest", API + "/latest", "1.2.3"),
            ("latest-prerelease", API + "?per_page=100", "1.3.0-dev.1.gabc123"),
            ("1.2.4", API + "/tags/1.2.4", "1.2.4"),
            ("latest-prerelease", API + "?per_page=100", "1.3.0-dev.2.gabc123"),
        ]
        for index, (selector, route, tag) in enumerate(steps):
            payload = release(tag, identifier=20 + index, prerelease=selector == "latest-prerelease")
            self.server.routes.setdefault(route, []).append(
                (200, [payload] if selector == "latest-prerelease" else payload, {}))
        playbook = self.work / "selector-transitions.yml"
        playbook.write_text(
            "---\n- hosts: fixture\n  gather_facts: false\n  connection: local\n"
            "  vars:\n    ansible_python_interpreter: '{{ ansible_playbook_python }}'\n"
            "    perimeterd_download_timeout: 10\n"
            f"    perimeterd_github_token: {SECRET}\n"
            "    _perimeterd_package_format: deb\n    _perimeterd_architecture: amd64\n"
            "  tasks:\n"
            + "".join(
                "    - ansible.builtin.include_role:\n"
                f"        name: {str(ROLE)!r}\n        tasks_from: resolve\n"
                f"      vars:\n        perimeterd_version: {selector!r}\n"
                "    - ansible.builtin.assert:\n"
                f"        that: _perimeterd_selected_tag == {tag!r}\n"
                for selector, _route, tag in steps))
        env = dict(os.environ, HTTPS_PROXY=f"http://127.0.0.1:{self.server.server_port}",
                   https_proxy=f"http://127.0.0.1:{self.server.server_port}",
                   HTTP_PROXY="", http_proxy="", ALL_PROXY="", all_proxy="",
                   NO_PROXY="", no_proxy="", SSL_CERT_FILE=str(self.cert),
                   ANSIBLE_NOCOLOR="1")
        result = subprocess.run(["ansible-playbook", "-i", "fixture,", str(playbook)],
                                cwd=ROLE, env=env, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertNotIn(SECRET, result.stdout)
        self.assertEqual([path for path, *_ in self.server.calls],
                         [route for _selector, route, _tag in steps])

    def test_host_specific_tokens_and_selectors_survive_serial_batches(self):
        tags = ("1.2.3", "0.0.1-dev.7.g607202599b29")
        for tag in tags:
            self.server.routes[f"{API}/tags/{tag}"] = (
                200, release(tag, prerelease=(tag != "1.2.3")), {})
        playbook = self.work / "heterogeneous.yml"
        playbook.write_text(
            "---\n- hosts: fixture_a,fixture_b\n  gather_facts: false\n"
            "  connection: local\n  serial: 1\n"
            "  vars:\n    ansible_python_interpreter: '{{ ansible_playbook_python }}'\n"
            "    perimeterd_download_timeout: 10\n"
            "    perimeterd_version: >-\n"
            "      {{ '1.2.3' if inventory_hostname == 'fixture_a' else '0.0.1-dev.7.g607202599b29' }}\n"
            "    perimeterd_github_token: >-\n"
            "      {{ 'fixture-token-a' if inventory_hostname == 'fixture_a' else 'fixture-token-b' }}\n"
            "    _perimeterd_package_format: deb\n    _perimeterd_architecture: amd64\n"
            "  tasks:\n    - ansible.builtin.include_role:\n"
            f"        name: {str(ROLE)!r}\n        tasks_from: resolve\n"
            "    - ansible.builtin.assert:\n"
            "        that: _perimeterd_selected_tag == perimeterd_version\n")
        env = dict(os.environ, HTTPS_PROXY=f"http://127.0.0.1:{self.server.server_port}",
                   https_proxy=f"http://127.0.0.1:{self.server.server_port}",
                   HTTP_PROXY="", http_proxy="", ALL_PROXY="", all_proxy="",
                   NO_PROXY="", no_proxy="", SSL_CERT_FILE=str(self.cert),
                   ANSIBLE_NOCOLOR="1")
        result = subprocess.run(
            ["ansible-playbook", "-i", "fixture_a,fixture_b,", str(playbook)],
            cwd=ROLE, env=env, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertNotIn("fixture-token-a", result.stdout)
        self.assertNotIn("fixture-token-b", result.stdout)
        self.assertEqual([(path, token) for path, token, _version in self.server.calls],
                         [(f"{API}/tags/{tags[0]}", "Bearer fixture-token-a"),
                          (f"{API}/tags/{tags[1]}", "Bearer fixture-token-b")])

    def test_rate_limit_and_malformed_json_are_distinct_errors(self):
        self.server.routes[API + "/latest"] = (
            403, {"message": "API rate limit exceeded"}, {"X-RateLimit-Remaining": "0"})
        self.assertIn("rate-limit", self.invoke(success=False).lower())
        self.server.routes[API + "/latest"] = (200, b"invalid json", {})
        self.assertIn("malformed JSON", self.invoke(success=False))
