"""Behavioral boundaries for owned acceleration, readiness and partial timings."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from vm import run as harness


class AcceleratorTests(unittest.TestCase):
    def test_native_aliases_select_kvm(self):
        for host, platform in (("x86_64", "debian-amd64"), ("amd64", "rocky-amd64"),
                               ("aarch64", "fedora-arm64"), ("arm64", "fedora-arm64")):
            with self.subTest(host=host), patch.object(harness.os, "uname", return_value=SimpleNamespace(machine=host)), \
                    patch.object(harness.os, "access", return_value=True):
                self.assertEqual(harness.select_accelerator(harness.IMAGES[platform], True), "kvm")

    def test_unavailable_or_inaccessible_kvm_requires_opt_in_tcg(self):
        with patch.object(harness.os, "uname", return_value=SimpleNamespace(machine="x86_64")), \
                patch.object(harness.os, "access", return_value=False):
            for platform in ("debian-amd64", "rocky-amd64"):
                with self.subTest(platform=platform):
                    image = harness.IMAGES[platform]
                    with self.assertRaisesRegex(RuntimeError, "/dev/kvm"):
                        harness.select_accelerator(image, True)
                    self.assertEqual(harness.select_accelerator(image), "tcg")

    def test_foreign_architecture_never_selects_kvm(self):
        for host, platform in (("x86_64", "fedora-arm64"), ("aarch64", "debian-amd64")):
            with self.subTest(host=host), patch.object(harness.os, "uname", return_value=SimpleNamespace(machine=host)), \
                    patch.object(harness.os, "access", return_value=True):
                image = harness.IMAGES[platform]
                with self.assertRaisesRegex(RuntimeError, "architectures"):
                    harness.select_accelerator(image, True)
                self.assertEqual(harness.select_accelerator(image), "tcg")

    def test_preflight_failure_does_not_fetch_or_boot(self):
        with tempfile.TemporaryDirectory() as work, \
                patch.object(sys, "argv", ["run.py", "--platform", "fedora-arm64", "--require-kvm", "--workdir", work]), \
                patch.object(harness.os, "uname", return_value=SimpleNamespace(machine="x86_64")), \
                patch.object(harness, "fetch_image") as fetch, patch.object(harness, "boot") as boot:
            with self.assertRaises(RuntimeError):
                harness.main()
            fetch.assert_not_called()
            boot.assert_not_called()
            records = [json.loads(line) for line in (Path(work) / "logs/timings.jsonl").read_text().splitlines()]
            self.assertEqual([(record["kind"], record["outcome"]) for record in records], [("run", "failed")])

    def test_external_required_kvm_is_rejected_before_work(self):
        with tempfile.TemporaryDirectory() as work:
            untouched = Path(work) / "not-created"
            completed = subprocess.run(
                [sys.executable, str(harness.ROLE / "tests/vm/run.py"), "--platform", "debian-amd64",
                 "--external-port", "1", "--identity", str(Path(work) / "missing-key"),
                 "--require-kvm", "--workdir", str(untouched)],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
            self.assertEqual(completed.returncode, 2)
            self.assertIn("--require-kvm", completed.stderr)
            self.assertIn("--external-port", completed.stderr)
            self.assertFalse(untouched.exists())


class GuestBoundaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)
        self.logs = self.work / "logs"
        self.logs.mkdir()
        self.timings = harness.Timings(self.logs, "debian-amd64", "published", "kvm")
        self.guest = harness.Guest(1, self.work / "key", self.logs, "debian-amd64",
                                   timings=self.timings, scenario="current-format")

    def records(self):
        return [json.loads(line) for line in self.timings.path.read_text().splitlines()]

    def test_dead_qemu_fails_without_ssh_or_sleep(self):
        process = Mock()
        process.poll.return_value = 1
        with patch.object(self.guest, "ssh") as ssh, patch.object(harness.time, "sleep") as sleep:
            with self.assertRaises(RuntimeError) as failure:
                self.guest.ready(process)
            self.assertIn(str(self.logs / "vm-console.log"), str(failure.exception))
            ssh.assert_not_called()
            sleep.assert_not_called()

    def test_qemu_death_after_failed_ssh_does_not_retry(self):
        process = Mock()
        process.poll.side_effect = [None, 1]
        with patch.object(self.guest, "ssh", side_effect=subprocess.CalledProcessError(255, "ssh")) as ssh, \
                patch.object(harness.time, "sleep") as sleep:
            with self.assertRaises(RuntimeError):
                self.guest.ready(process)
            self.assertEqual(ssh.call_count, 1)
            sleep.assert_not_called()

    def test_play_outcomes_include_assertions_and_resolved_version(self):
        cases = (
            ("expected", 2, "rejected", {"fails": True}, None, "expected_failure"),
            ("unexpected-success", 0, "target : changed=0", {"fails": True}, AssertionError, "failed"),
            ("unexpected-failure", 2, "rejected", {}, AssertionError, "failed"),
            ("missing-recap", 0, "no recap", {}, AssertionError, "failed"),
            ("changed-noop", 0, "target : changed=1", {"noop": True}, AssertionError, "failed"),
            ("missing-tag", 0, "target : changed=0", {}, AssertionError, "failed"),
            ("resolved", 0, "target : changed=1\nRESOLVED_TAG=0.0.1-dev.8", {}, None, "passed"),
            ("pinned-noop", 0, "target : changed=0", {"noop": True}, None, "passed"),
        )
        for label, status, output, options, error, outcome in cases:
            with self.subTest(label=label), patch.object(
                    harness.subprocess, "run", return_value=subprocess.CompletedProcess([], status, output)):
                if error:
                    with self.assertRaises(error):
                        self.guest.play(label, **options)
                else:
                    self.assertEqual(self.guest.play(label, **options), output)
                record = self.records()[-1]
                self.assertEqual(record["outcome"], outcome)
                self.assertEqual(record["scenario"], "current-format")
                self.assertEqual(record["version"], "0.0.1-dev.8" if label in ("resolved", "pinned-noop") else None)
                self.assertEqual((self.logs / f"{label}.log").read_text(), output)

    def test_timeout_retains_output_and_original_exception(self):
        error = subprocess.TimeoutExpired("ansible-playbook", 1000, output=b"partial\xff output")
        with patch.object(harness.subprocess, "run", side_effect=error):
            with self.assertRaises(subprocess.TimeoutExpired) as failure:
                self.guest.play("timeout")
        self.assertIs(failure.exception, error)
        self.assertEqual(self.records()[-1]["outcome"], "timeout")
        self.assertEqual((self.logs / "timeout.log").read_text(), "partial\ufffd output")

    def test_nested_failure_totals_include_cleanup_and_preserve_exception(self):
        now = [0.0]
        error = RuntimeError("lifecycle failed")
        with patch.object(harness.time, "monotonic", side_effect=lambda: now[0]):
            with self.assertRaises(RuntimeError) as failure:
                with self.timings.measure("scenario", "current-format", scenario="current-format"):
                    try:
                        with self.timings.measure("phase", "lifecycle", scenario="current-format"):
                            now[0] = 4.0
                            raise error
                    finally:
                        with self.timings.measure("phase", "cleanup", scenario="current-format"):
                            now[0] = 7.0
        self.assertIs(failure.exception, error)
        self.assertEqual([(record["label"], record["elapsed_seconds"], record["outcome"]) for record in self.records()],
                         [("lifecycle", 4.0, "failed"), ("cleanup", 3.0, "passed"), ("current-format", 7.0, "failed")])

    def test_reporting_failure_does_not_replace_original_failure(self):
        error = KeyboardInterrupt()
        with patch.object(Path, "open", side_effect=OSError("disk full")):
            with self.assertRaises(KeyboardInterrupt) as failure:
                with self.timings.measure("phase", "lifecycle"):
                    raise error
        self.assertIs(failure.exception, error)
