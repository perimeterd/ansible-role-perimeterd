"""Candidate handoff rejects wrong bytes and native identity before booting a VM."""
import hashlib
import http.client
import json
from pathlib import Path
import shutil
import ssl
import subprocess
import tempfile
import unittest

from vm.candidate import API, Candidate, PackageFixture, asset_name
from .test_vm_harness import owned_run

VERSION = "0.0.1-dev.8.g123456789abc"


@unittest.skipUnless(shutil.which("dpkg-deb"), "dpkg-deb is required for real native artifact fixtures")
class CandidateArtifact(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="perimeterd-candidate-artifact-")
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name)
        self.dist = self.work / "dist"
        self.dist.mkdir()
        (self.dist / "metadata.json").write_text(json.dumps({"project_name": "perimeterd", "version": VERSION}))
        self.package = self.dist / asset_name(VERSION, "debian-amd64")
        self.build()

    def build(self, version=None, architecture="amd64"):
        root = self.work / "package"
        control = root / "DEBIAN"
        control.mkdir(parents=True, exist_ok=True)
        (control / "control").write_text(
            "Package: perimeterd\nVersion: " + (version or VERSION.replace("-dev", "~dev") + "-1") +
            f"\nArchitecture: {architecture}\nMaintainer: Test <test@example.invalid>\nDescription: Native identity boundary fixture\n")
        subprocess.run(["dpkg-deb", "--build", "--root-owner-group", str(root), str(self.package)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.manifest = hashlib.sha256(self.package.read_bytes()).hexdigest() + "  " + self.package.name + "\n"
        (self.dist / "checksums.txt").write_text(self.manifest)

    def test_package_bytes_mismatch_is_rejected(self):
        with self.package.open("ab") as output:
            output.write(b"changed after release manifest")
        with self.assertRaises(ValueError):
            Candidate(self.dist, "debian-amd64")

    def test_missing_manifest_is_rejected(self):
        (self.dist / "checksums.txt").unlink()
        with self.assertRaises(FileNotFoundError):
            Candidate(self.dist, "debian-amd64")

    def test_missing_selected_record_is_rejected(self):
        (self.dist / "checksums.txt").write_text("a" * 64 + "  unrelated.deb\n")
        with self.assertRaises(ValueError):
            Candidate(self.dist, "debian-amd64")

    def test_duplicate_selected_record_is_rejected(self):
        (self.dist / "checksums.txt").write_text(self.manifest * 2)
        with self.assertRaises(ValueError):
            Candidate(self.dist, "debian-amd64")

    def test_correct_checksum_wrong_native_version_is_rejected(self):
        self.build(version="0.0.1~dev.99-1")
        with self.assertRaises(ValueError):
            Candidate(self.dist, "debian-amd64")

    def test_correct_checksum_wrong_native_architecture_is_rejected(self):
        self.build(architecture="arm64")
        with self.assertRaises(ValueError):
            Candidate(self.dist, "debian-amd64")

    def test_invalid_project_or_version_metadata_is_rejected(self):
        for metadata in ({"project_name": "other", "version": VERSION},
                         {"project_name": "perimeterd", "version": "../release"}):
            with self.subTest(metadata=metadata):
                (self.dist / "metadata.json").write_text(json.dumps(metadata))
                with self.assertRaises(ValueError):
                    Candidate(self.dist, "debian-amd64")


    @unittest.skipUnless(shutil.which("openssl"), "openssl is required for trusted TLS transport")
    def test_scenario_selection_preserves_validated_candidate_and_scoped_trust(self):
        for selection, expected in ((None, ["current-format", "fresh-stale-inode"]),
                                    ("fresh-stale-inode", ["fresh-stale-inode"])):
            with self.subTest(selection=selection):
                work = self.work / (selection or "both")
                arguments = ["--candidate-dist", str(self.dist)]
                if selection:
                    arguments += ["--scenario", selection]
                seen, records = owned_run(work, arguments)
                self.assertEqual([row[0] for row in seen], expected)
                self.assertEqual({row[1] for row in seen}, {VERSION})
                for _scenario, _version, candidate, fixture in seen:
                    self.assertEqual(candidate.package, self.package.read_bytes())
                    self.assertEqual(candidate.manifest, self.manifest.encode())
                    self.assertFalse(fixture.cert.exists())
                self.assertEqual({record["version"] for record in records}, {VERSION})
                self.assertEqual({record["mode"] for record in records}, {"candidate"})

    def fixture_get(self, fixture, host, path, header_host=None):
        context = ssl.create_default_context(cafile=str(fixture.cert))
        connection = http.client.HTTPSConnection("127.0.0.1", fixture.port, context=context, timeout=5)
        self.addCleanup(connection.close)
        connection.set_tunnel(host, 443)
        connection.request("GET", path, headers={"Host": header_host or host})
        response = connection.getresponse()
        return response.status, response.read()

    @unittest.skipUnless(shutil.which("openssl"), "openssl is required for trusted TLS transport")
    def test_original_https_routes_preserve_selected_identity_and_bytes(self):
        candidate = Candidate(self.dist, "debian-amd64")
        fixture = PackageFixture(candidate, self.work)
        self.addCleanup(fixture.close)
        status, body = self.fixture_get(fixture, "api.github.com", f"{API}/tags/{VERSION}")
        self.assertEqual(status, 200)
        metadata = json.loads(body)
        self.assertEqual(metadata["tag_name"], VERSION)
        self.assertFalse(metadata["draft"])
        self.assertEqual({asset["name"] for asset in metadata["assets"]}, {"checksums.txt", candidate.name})
        for asset in metadata["assets"]:
            url = f"/perimeterd/perimeterd/releases/download/{VERSION}/{asset['name']}"
            self.assertEqual(asset["browser_download_url"], "https://github.com" + url)
            status, body = self.fixture_get(fixture, "github.com", url)
            self.assertEqual(status, 200)
            self.assertEqual(body, candidate.manifest if asset["name"] == "checksums.txt" else candidate.package)
            self.assertEqual(asset["size"], len(body))

    @unittest.skipUnless(shutil.which("openssl"), "openssl is required for trusted TLS transport")
    def test_unselected_routes_and_host_boundaries_are_rejected(self):
        fixture = PackageFixture(Candidate(self.dist, "debian-amd64"), self.work)
        self.addCleanup(fixture.close)
        selected = f"{API}/tags/{VERSION}"
        for host, path, header in (
                ("api.github.com", f"{API}/tags/0.0.1-dev.99", None),
                ("api.github.com", f"{API}/tags/{VERSION}/extra", None),
                ("github.com", selected, None),
                ("api.github.com", selected, "github.com")):
            with self.subTest(host=host, path=path, header=header):
                self.assertEqual(self.fixture_get(fixture, host, path, header)[0], 404)
        with self.assertRaisesRegex(OSError, "403"):
            self.fixture_get(fixture, "untrusted.example", selected)
