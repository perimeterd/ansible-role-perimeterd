"""Real Git/OpenPGP admission and immutable role-release publication boundaries."""
import copy
import http.server
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest import mock
import urllib.error

from scripts import release


@unittest.skipUnless(shutil.which("git") and shutil.which("gpg"), "Git and GPG are required")
class SignedTagAdmission(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys = tempfile.TemporaryDirectory(prefix="perimeterd-role-signers-")
        cls.addClassCleanup(cls.keys.cleanup)
        cls.signers = []
        for name in ("trusted", "other"):
            home = Path(cls.keys.name) / name
            home.mkdir(mode=0o700)
            env = dict(os.environ, GNUPGHOME=str(home))
            subprocess.run(["gpg", "--batch", "--pinentry-mode", "loopback", "--passphrase", "",
                            "--quick-generate-key", f"{name} <{name}@example.invalid>", "ed25519", "sign", "0"],
                           env=env, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            cls.addClassCleanup(subprocess.run, ["gpgconf", "--homedir", str(home), "--kill", "gpg-agent"],
                                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            listing = subprocess.check_output(["gpg", "--with-colons", "--list-keys"], text=True, env=env)
            fingerprint = next(line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:"))
            public = subprocess.check_output(["gpg", "--armor", "--export", fingerprint], text=True, env=env)
            cls.signers.append((env, fingerprint, public))

    def setUp(self):
        work = tempfile.TemporaryDirectory(prefix="perimeterd-role-release-git-")
        self.addCleanup(work.cleanup)
        previous = os.getcwd()
        os.chdir(work.name)
        self.addCleanup(os.chdir, previous)
        self.git("init", "--quiet", "-b", "main")
        self.git("config", "user.name", "Role release fixture")
        self.git("config", "user.email", "role@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "tag.gpgsign", "false")
        self.git("commit", "--quiet", "--allow-empty", "-m", "reviewed role")
        self.commit = self.git("rev-parse", "HEAD")
        self.git("update-ref", "refs/remotes/origin/main", self.commit)

    def git(self, *args, env=None):
        return subprocess.check_output(["git", *args], text=True, env=env, stderr=subprocess.PIPE).strip()

    def sign(self, tag="1.2.3", signer=0):
        env, fingerprint, _ = self.signers[signer]
        self.git("-c", "gpg.format=openpgp", "-c", "gpg.program=gpg", "-c", f"user.signingkey={fingerprint}",
                 "tag", "-s", tag, "-m", f"Release {tag}", env=env)

    def admit(self, tag="1.2.3", sha=None, keys=None):
        return release.admit(f"refs/tags/{tag}", sha or self.git("rev-parse", "HEAD"),
                             self.signers[0][2] if keys is None else keys)

    def test_trusted_signed_tag_remains_eligible_after_main_advances(self):
        self.sign()
        self.git("commit", "--quiet", "--allow-empty", "-m", "later reviewed change")
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        self.git("checkout", "--quiet", "--detach", "1.2.3")
        tag_object = self.git("rev-parse", "refs/tags/1.2.3")
        self.assertEqual(self.admit(sha=tag_object), {"tag": "1.2.3", "commit": self.commit})

    def test_only_canonical_stable_tag_refs_are_eligible(self):
        for ref in ("refs/heads/main", "refs/tags/v1.2.3", "refs/tags/01.2.3",
                    "refs/tags/1.2.3-rc.1", "refs/tags/1.2.3+build"):
            with self.subTest(ref=ref), self.assertRaises(ValueError):
                release.admit(ref, self.commit, self.signers[0][2])

    def test_lightweight_tag_cannot_authorize_publication(self):
        self.git("tag", "1.2.3")
        with self.assertRaises(ValueError):
            self.admit()

    def test_unsigned_annotation_cannot_authorize_publication(self):
        self.git("tag", "-a", "1.2.3", "-m", "unsigned role release")
        with self.assertRaises(subprocess.CalledProcessError):
            self.admit()

    def test_missing_configured_key_cannot_use_ambient_trust(self):
        self.sign()
        with mock.patch.dict(os.environ, self.signers[0][0]), self.assertRaises(ValueError):
            self.admit(keys="")

    def test_other_signer_is_rejected_even_when_ambient_keyring_trusts_it(self):
        self.sign(signer=1)
        with mock.patch.dict(os.environ, self.signers[1][0]), self.assertRaises(subprocess.CalledProcessError):
            self.admit()

    @unittest.skipUnless(shutil.which("ssh-keygen"), "ssh-keygen is required for real SSH signatures")
    def test_ssh_signature_cannot_use_ambient_allowed_signers(self):
        private = Path.cwd() / "ssh-signer"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(private)], check=True)
        allowed = Path.cwd() / "allowed-signers"
        allowed.write_text("role@example.invalid " + Path(str(private) + ".pub").read_text())
        self.git("config", "gpg.ssh.allowedSignersFile", str(allowed))
        self.git("-c", "gpg.format=ssh", "-c", f"user.signingkey={private}",
                 "tag", "-s", "1.2.3", "-m", "SSH-signed role release")
        with self.assertRaises(ValueError):
            self.admit()

    def test_another_version_cannot_reuse_a_signed_tag_annotation(self):
        self.sign()
        self.git("update-ref", "refs/tags/1.2.4", "refs/tags/1.2.3")
        with self.assertRaises(ValueError):
            self.admit("1.2.4")

    def test_signed_unmerged_commit_is_rejected(self):
        self.git("checkout", "--quiet", "-b", "unreviewed")
        self.git("commit", "--quiet", "--allow-empty", "-m", "unreviewed role")
        self.sign()
        with self.assertRaises(subprocess.CalledProcessError):
            self.admit()

    def test_checkout_must_match_triggering_commit(self):
        self.sign()
        self.git("commit", "--quiet", "--allow-empty", "-m", "different checkout")
        with self.assertRaises(ValueError):
            self.admit(sha=self.commit)

    def test_tag_must_match_tested_checkout(self):
        self.sign()
        self.git("commit", "--quiet", "--allow-empty", "-m", "different tested commit")
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        with self.assertRaises(ValueError):
            self.admit()


class ReleasePublication(unittest.TestCase):
    def setUp(self):
        self.tag = "1.2.3"
        self.commit = "a" * 40
        self.tag_object = "c" * 40
        self.remote_tag = {"ref": "refs/tags/1.2.3", "object": {"type": "tag", "sha": self.tag_object}}
        self.remote_tag_status = 200
        self.document = None
        self.lookup_status = None
        self.post_status = 201
        self.requests = []
        fixture = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def respond(self, status, body):
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                if status == 302:
                    self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/capture")
                self.end_headers()
                self.wfile.write(json.dumps(body).encode())

            def authorized(self):
                fixture.requests.append((self.command, self.path))
                if self.headers.get("Authorization") != "Bearer fixture-token":
                    self.respond(401, {"message": "Unauthorized"})
                    return False
                return True

            def do_GET(self):
                if not self.authorized():
                    return
                if self.path == "/repos/example/role/git/ref/tags/1.2.3":
                    self.respond(fixture.remote_tag_status, fixture.remote_tag)
                elif self.path != "/repos/example/role/releases/tags/1.2.3":
                    self.respond(404, {"message": "Unknown endpoint"})
                elif fixture.lookup_status:
                    self.respond(fixture.lookup_status, {"message": "Lookup failed"})
                elif fixture.document is None:
                    self.respond(404, {"message": "Not Found"})
                else:
                    self.respond(200, fixture.document)

            def do_POST(self):
                if not self.authorized():
                    return
                if self.path != "/repos/example/role/releases":
                    self.respond(404, {"message": "Unknown endpoint"})
                    return
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fixture.document = {**body, "target_commitish": "main", "id": 7,
                                    "html_url": "https://github.com/example/role/releases/tag/1.2.3"}
                self.respond(fixture.post_status, fixture.document)

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.patch = mock.patch.object(release, "API", f"http://127.0.0.1:{server.server_port}")
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def publish(self):
        return release.publish(self.tag, self.tag_object, "example/role", "fixture-token")

    def matching_release(self):
        return {"tag_name": self.tag, "target_commitish": self.commit, "draft": False,
                "prerelease": False, "id": 7, "html_url": "https://github.com/example/role/releases/tag/1.2.3"}

    def test_creates_stable_release_and_rerun_reuses_without_mutation(self):
        first = self.publish()
        published = copy.deepcopy(self.document)
        self.assertEqual(self.publish(), first)
        self.assertEqual(self.document, published)
        self.assertEqual(sum(method == "POST" for method, _ in self.requests), 1)
        self.assertIs(self.document["draft"], False)
        self.assertIs(self.document["prerelease"], False)

    def test_conflicting_identity_or_channel_is_never_edited(self):
        for field, wrong in (("tag_name", "2.0.0"), ("draft", True), ("prerelease", True)):
            with self.subTest(field=field):
                self.document = {**self.matching_release(), field: wrong}
                before = copy.deepcopy(self.document)
                with self.assertRaises(ValueError):
                    self.publish()
                self.assertEqual(self.document, before)
        self.assertTrue(all(method == "GET" for method, _ in self.requests))

    def test_remote_tag_change_is_rejected_before_release_creation(self):
        self.remote_tag["object"]["sha"] = "d" * 40
        with self.assertRaises(ValueError):
            self.publish()
        self.assertIsNone(self.document)
        self.assertTrue(all(method == "GET" for method, _ in self.requests))

    def test_unsigned_remote_ref_cannot_replace_verified_annotation(self):
        self.remote_tag["object"]["type"] = "commit"
        with self.assertRaises(ValueError):
            self.publish()
        self.assertIsNone(self.document)
        self.assertTrue(all(method == "GET" for method, _ in self.requests))

    def test_missing_remote_tag_cannot_create_an_unsigned_ref_or_release(self):
        self.remote_tag_status = 404
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.publish()
        self.assertEqual(error.exception.code, 404)
        self.assertIsNone(self.document)
        self.assertTrue(all(method == "GET" for method, _ in self.requests))

    def test_branch_target_metadata_does_not_override_verified_tag(self):
        self.document = {**self.matching_release(), "target_commitish": "main"}
        before = copy.deepcopy(self.document)
        self.publish()
        self.assertEqual(self.document, before)
        self.assertTrue(all(method == "GET" for method, _ in self.requests))

    def test_lookup_failure_is_not_treated_as_a_missing_release(self):
        for status in (403, 503):
            with self.subTest(status=status):
                self.lookup_status = status
                with self.assertRaises(urllib.error.HTTPError) as error:
                    self.publish()
                self.assertEqual(error.exception.code, status)
        self.assertIsNone(self.document)
        self.assertTrue(all(method == "GET" for method, _ in self.requests))

    def test_lost_creation_completion_is_recovered_only_on_operator_rerun(self):
        self.post_status = 503
        with self.assertRaises(urllib.error.HTTPError):
            self.publish()
        created = copy.deepcopy(self.document)
        self.assertEqual(sum(method == "POST" for method, _ in self.requests), 1)
        self.publish()
        self.assertEqual(self.document, created)
        self.assertEqual(sum(method == "POST" for method, _ in self.requests), 1)

    def test_redirect_is_not_followed_with_publication_credentials(self):
        self.lookup_status = 302
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.publish()
        self.assertEqual(error.exception.code, 302)
        self.assertTrue(all(path != "/capture" for _, path in self.requests))
        self.assertIsNone(self.document)

    def test_missing_token_cannot_send_a_publication_request(self):
        with self.assertRaises(ValueError):
            release.publish(self.tag, self.tag_object, "example/role", "")
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
