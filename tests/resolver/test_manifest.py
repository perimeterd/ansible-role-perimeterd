"""Reject malicious release manifests before any managed-host package mutation."""

from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import ssl
import subprocess
import tempfile
import threading
import unittest

from resolver.test_selection import FixtureProxy, ROLE, release

TAG = "0.0.1-dev.7.g607202599b29"
PATH = f"/perimeterd/perimeterd/releases/download/{TAG}/checksums.txt"
PACKAGE = f"perimeterd_{TAG}-1_amd64.deb"
DIGEST = "a" * 64


class ChecksumManifestWithAnsible(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="perimeterd-checksum-fixture-")
        cls.work = Path(cls.temporary.name)
        key, cert = cls.work / "tls.key", cls.work / "tls.crt"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-days", "1", "-subj", "/CN=github.com",
                        "-addext", "subjectAltName=DNS:github.com",
                        "-keyout", str(key), "-out", str(cert)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureProxy)
        cls.server.allowed_host = "github.com"
        cls.server.routes = {}
        cls.server.calls = []
        cls.server.tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        cls.server.tls_context.load_cert_chain(certfile=cert, keyfile=key)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.cert = cert
        upstream = release(TAG, prerelease=True)
        cls.playbook = cls.work / "manifest.yml"
        cls.playbook.write_text(
            "---\n- hosts: fixture\n  gather_facts: false\n  connection: local\n"
            "  vars:\n    ansible_python_interpreter: '{{ ansible_playbook_python }}'\n"
            "    perimeterd_download_timeout: 10\n"
            "    perimeterd_allow_downgrade: false\n"
            "    _perimeterd_package_format: deb\n    _perimeterd_architecture: amd64\n"
            f"    _perimeterd_selected_tag: {TAG}\n"
            f"    _perimeterd_release: {json.dumps(upstream)}\n"
            f"    _perimeterd_asset: {json.dumps(upstream['assets'][1])}\n"
            f"    _perimeterd_checksums_asset: {json.dumps(upstream['assets'][0])}\n"
            "  tasks:\n    - ansible.builtin.setup:\n"
            "        gather_subset: ['!all', 'min']\n"
            "    - ansible.builtin.include_role:\n"
            f"        name: {str(ROLE)!r}\n        tasks_from: install\n")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=10)
        cls.temporary.cleanup()

    def setUp(self):
        self.server.routes = {}
        self.server.calls = []

    def reject(self, manifest):
        self.server.routes[PATH] = (200, manifest.encode(), {})
        env = dict(os.environ, HTTPS_PROXY=f"http://127.0.0.1:{self.server.server_port}",
                   https_proxy=f"http://127.0.0.1:{self.server.server_port}",
                   HTTP_PROXY="", http_proxy="", ALL_PROXY="", all_proxy="",
                   NO_PROXY="", no_proxy="", SSL_CERT_FILE=str(self.cert),
                   ANSIBLE_NOCOLOR="1")
        result = subprocess.run(
            ["ansible-playbook", "-i", "fixture,", str(self.playbook), "--check"],
            cwd=ROLE, env=env, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, timeout=120)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual([path for path, *_ in self.server.calls], [PATH])

    def test_duplicate_selected_digest_is_not_accepted(self):
        self.reject(f"{DIGEST}  {PACKAGE}\n{'b' * 64}  {PACKAGE}\n")

    def test_missing_selected_digest_is_not_an_unverified_download(self):
        self.reject(f"{DIGEST}  other-asset.deb\n")

    def test_unsafe_manifest_record_is_rejected(self):
        self.reject(f"{DIGEST}  {PACKAGE}\n{DIGEST}  ../traversal.deb\n")

    def test_malformed_manifest_record_is_rejected(self):
        self.reject(f"{DIGEST}  {PACKAGE}\nnot-a-sha  other.deb\n")
