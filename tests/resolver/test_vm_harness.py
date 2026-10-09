"""Behavioral boundaries for owned acceleration, readiness and partial timings."""
from contextlib import ExitStack
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from vm import run as harness


def owned_run(work, arguments=(), *, failure=None):
    """Exercise owned resource orchestration with a minimal play lifecycle."""
    seen, processes, overlays, fixtures = [], [], [], []
    tag = "v1.2.3-dev.4+build.05"

    def boot(image, disk, directory, key, accelerator):
        overlays.append(directory)
        assert not (directory / "installed-state").exists()
        process = Mock()
        process.poll.return_value = None
        processes.append(process)
        return process, 22, (directory.parent / "logs" / "vm-console.log").open("w")

    def execute(guest, platform):
        directory = overlays[-1]
        seen.append((guest.scenario, guest.version, guest.candidate, guest.fixture))
        if guest.fixture:
            fixtures.append(guest.fixture)
            assert guest.fixture.cert.exists()
            assert guest.controller_environment["SSL_CERT_FILE"] == str(guest.fixture.cert)
        guest.play("baseline")
        (directory / "installed-state").write_text(guest.version)
        if failure:
            raise failure

    with ExitStack() as stack:
        stack.enter_context(patch.object(sys, "argv", [
            "run.py", "--platform", "debian-amd64", "--workdir", str(work), *arguments]))
        stack.enter_context(patch.object(harness, "fetch_image", return_value=work / "base.qcow2"))
        stack.enter_context(patch.object(harness, "select_accelerator", return_value="kvm"))
        stack.enter_context(patch.object(harness, "boot", side_effect=boot))
        stack.enter_context(patch.object(harness.Guest, "ready"))
        stack.enter_context(patch.object(harness.Guest, "ssh", return_value="test stack"))
        stack.enter_context(patch.object(harness.Guest, "connect_fixture"))
        stack.enter_context(patch.dict(harness.SCENARIOS, {
            "current-format": execute, "fresh-stale-inode": execute}))
        original_run = harness.subprocess.run

        def subprocess_run(argv, **kwargs):
            if argv[0] == "ansible-playbook":
                return subprocess.CompletedProcess(argv, 0, f"target : changed=0\nRESOLVED_TAG={tag}")
            return original_run(argv, **kwargs)

        stack.enter_context(patch.object(harness.subprocess, "run", side_effect=subprocess_run))
        try:
            harness.main()
        finally:
            for directory, process in zip(overlays, processes):
                assert not directory.exists(), "owned overlay survived teardown"
                process.terminate.assert_called_once()
                process.wait.assert_called_once()
            assert not tuple(work.glob("vm-keys-*")), "SSH keys survived shared teardown"
            for fixture in fixtures:
                assert not fixture.cert.exists(), "fixture trust survived shared teardown"
    records = [json.loads(line) for line in (work / "logs/timings.jsonl").read_text().splitlines()]
    return seen, records


class ScenarioTests(unittest.TestCase):
    def test_default_discovers_once_and_reuses_tag_in_isolated_overlays(self):
        with tempfile.TemporaryDirectory() as directory:
            seen, records = owned_run(Path(directory))
        self.assertEqual([(scenario, version) for scenario, version, *_ in seen],
                         [("current-format", harness.CURRENT), ("fresh-stale-inode", "v1.2.3-dev.4+build.05")])
        self.assertEqual([(r["label"], r["version"], r["outcome"]) for r in records if r["kind"] == "scenario"],
                         [("current-format", "v1.2.3-dev.4+build.05", "passed"),
                          ("fresh-stale-inode", "v1.2.3-dev.4+build.05", "passed")])

    def test_selected_scenario_is_pristine_and_exact_tag_is_baseline(self):
        for scenario in harness.SCENARIOS:
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                work = Path(directory)
                seen, records = owned_run(work, ["--scenario", scenario, "--release-tag", "v1.2.3+build.01"])
                self.assertEqual([(name, version) for name, version, *_ in seen], [(scenario, "v1.2.3+build.01")])
                self.assertFalse((work / next(name for name in harness.SCENARIOS if name != scenario)).exists())
                self.assertEqual({r["version"] for r in records}, {"v1.2.3+build.01"})
                self.assertEqual({r["scenario"] for r in records if r["kind"] == "scenario"}, {scenario})

    def test_selected_failure_cleans_up_and_retains_failed_timings(self):
        error = RuntimeError("lifecycle failure")
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            with self.assertRaises(RuntimeError) as result:
                owned_run(work, ["--scenario", "fresh-stale-inode"], failure=error)
            self.assertIs(result.exception, error)
            records = [json.loads(line) for line in (work / "logs/timings.jsonl").read_text().splitlines()]
            self.assertEqual([(r["label"], r["outcome"]) for r in records if r["kind"] in ("scenario", "run")],
                             [("fresh-stale-inode", "failed"), ("harness", "failed")])
            self.assertTrue((work / "fresh-stale-inode/logs/guest-daemon-journal.log").exists())

    def test_external_default_retains_one_lifecycle_and_exact_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            with patch.object(sys, "argv", [
                    "run.py", "--platform", "debian-amd64", "--workdir", str(work),
                    "--external-port", "22", "--identity", str(work / "key"), "--release-tag", "v1.2.3-dev.4"]), \
                    patch.object(harness, "exercise") as exercise, patch.object(harness, "boot") as boot:
                harness.main()
            guest = exercise.call_args.args[0]
            self.assertEqual(guest.version, "v1.2.3-dev.4")
            boot.assert_not_called()
            records = [json.loads(line) for line in (work / "logs/timings.jsonl").read_text().splitlines()]
            self.assertEqual([(r["kind"], r["accelerator"], r["version"]) for r in records],
                             [("phase", "external", "v1.2.3-dev.4"), ("run", "external", "v1.2.3-dev.4")])

    def test_invalid_arguments_fail_before_any_work_or_contact(self):
        cases = [["--scenario", "unknown"],
                 ["--release-tag", "1.2.3", "--candidate-dist", "/missing/dist"],
                 ["--candidate-dist", "/missing/dist", "--external-port", "22"]]
        cases += [["--release-tag", tag] for tag in (
            "", "latest", "latest-prerelease", "1.2", "01.2.3", "1.2.3-01", "1.2.3-a..b",
            " 1.2.3", "1.2.3 ", "1.2.3\n", "1.2.3\ninjected=output", "1.2.3+")]
        cases += [["--external-port", "22", "--scenario", scenario] for scenario in ("all", *harness.SCENARIOS)]
        for arguments in cases:
            with self.subTest(arguments=arguments), tempfile.TemporaryDirectory() as directory:
                work = Path(directory) / "untouched"
                result = subprocess.run(
                    [sys.executable, str(harness.ROLE / "tests/vm/run.py"), "--platform", "debian-amd64",
                     "--workdir", str(work), "--identity", "/missing/key", *arguments],
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertFalse(work.exists())

    def test_supported_exact_tag_forms_are_not_normalized(self):
        for tag in ("0.0.0", "1.2.3", "v1.2.3", "1.2.3-0", "1.2.3-dev.12",
                    "v1.2.3-rc.1+build.01", "1.2.3-01a", "1.2.3+001"):
            with self.subTest(tag=tag):
                self.assertEqual(harness.release_tag(tag), tag)


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
