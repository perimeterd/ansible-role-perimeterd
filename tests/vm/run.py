#!/usr/bin/env python3
"""Run the real upstream package and systemd smoke matrix in disposable VMs."""

import argparse
from contextlib import ExitStack
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import subprocess
import tempfile
import time
from urllib.request import urlopen
from candidate import Candidate, PackageFixture


@dataclass(frozen=True)
class Image:
    url: str
    sha256: str
    filename: str
    architecture: str


IMAGES = {
    "debian-amd64": Image(
        "https://cloud.debian.org/images/cloud/trixie/20260914-2601/debian-13-generic-amd64-20260914-2601.qcow2",
        "b6e3a4dac69b38d55a763b1752e8fd7bb12e3948672a79fee4ef13fa53839d2f",
        "debian13-amd64.qcow2", "amd64"),
    "fedora-arm64": Image(
        "https://download.fedoraproject.org/pub/fedora/linux/releases/44/Cloud/aarch64/images/Fedora-Cloud-Base-Generic-44-1.7.aarch64.qcow2",
        "55c60a3b80d3616a08705afd0459e75fe9f03c54aba7a46e4002a41a72fa0d5b",
        "fedora-aarch64.qcow2", "arm64"),
    "rocky-amd64": Image(
        "https://download.rockylinux.org/pub/rocky/8/images/x86_64/Rocky-8-GenericCloud-Base-8.10-20240528.0.x86_64.qcow2",
        "e56066c58606191e96184de9a9183a3af33c59bcbd8740d8b10ca054a7a89c14",
        "rocky8-x86_64.qcow2", "amd64"),
}

CURRENT = "latest-prerelease"
BASE_CONFIG = {"version": 1, "firewall": {"backend": "nftables"}, "policies": []}
ROLE = Path(__file__).resolve().parents[2]
SSH_OPTIONS = ("-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
               "-o", "BatchMode=yes", "-o", "ConnectTimeout=8")


def run(argv, *, timeout=180, **kwargs):
    return subprocess.run(argv, check=True, timeout=timeout, text=True, **kwargs)


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch_image(image, cache):
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / image.filename
    if target.exists() and sha256(target) == image.sha256:
        print(f"verified cached image {target.name}", flush=True)
        return target
    temporary = target.with_suffix(target.suffix + ".partial")
    try:
        with urlopen(image.url, timeout=120) as response, temporary.open("wb") as output:
            shutil.copyfileobj(response, output)
        if sha256(temporary) != image.sha256:
            raise ValueError(f"cloud image checksum mismatch: {image.filename}")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"downloaded and verified {target.name}", flush=True)
    return target


def boot(image, disk, work, key):
    overlay = work / "guest.qcow2"
    run(["qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b", str(disk), str(overlay), "10G"])
    pubkey = key.with_suffix(key.suffix + ".pub").read_text().strip().split(" ", 2)[:2]
    (work / "user-data").write_text(
        "#cloud-config\nssh_pwauth: false\ndisable_root: true\nusers:\n"
        "  - default\n  - name: ansible\n    shell: /bin/bash\n"
        "    lock_passwd: true\n    sudo: 'ALL=(ALL) NOPASSWD:ALL'\n"
        f"    ssh_authorized_keys:\n      - {' '.join(pubkey)}\n"
        "growpart:\n  mode: auto\n  devices: ['/']\nresize_rootfs: true\n"
    )
    (work / "meta-data").write_text(
        f"instance-id: perimeterd-{image.filename}-{time.time_ns()}\n"
        "local-hostname: perimeterd-role-smoke\n")
    seed = work / "seed.iso"
    run(["genisoimage", "-quiet", "-output", str(seed), "-volid", "cidata",
         "-joliet", "-rock", str(work / "user-data"), str(work / "meta-data")])
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    network = ("-netdev", f"user,id=network,hostfwd=tcp:127.0.0.1:{port}-:22",
               "-device", "virtio-net-pci,netdev=network")
    if image.architecture == "amd64":
        machine = "q35,accel=kvm" if os.access("/dev/kvm", os.R_OK | os.W_OK) else "q35,accel=tcg"
        argv = ["qemu-system-x86_64", "-machine", machine, "-cpu", "host" if "kvm" in machine else "max",
                "-smp", "2", "-m", "2048", "-drive", f"file={overlay},format=qcow2,if=virtio",
                "-drive", f"file={seed},format=raw,readonly=on,if=virtio,media=cdrom"]
    else:
        firmware = next((f for f in ("/usr/share/qemu-efi-aarch64/QEMU_EFI.fd",
                                    "/usr/share/edk2/aarch64/QEMU_EFI.fd") if Path(f).exists()), None)
        if not firmware:
            raise RuntimeError("install qemu-efi-aarch64 or edk2-aarch64 to boot the arm64 VM")
        native_arm = os.uname().machine in ("aarch64", "arm64") and os.access("/dev/kvm", os.R_OK | os.W_OK)
        argv = ["qemu-system-aarch64", "-machine", "virt,accel=kvm" if native_arm else "virt",
                "-cpu", "host" if native_arm else "cortex-a72", "-smp", "2", "-m", "2048",
                "-bios", firmware, "-drive", f"if=none,file={overlay},format=qcow2,id=main",
                "-device", "virtio-blk-pci,drive=main",
                "-drive", f"if=none,file={seed},format=raw,readonly=on,id=seed",
                "-device", "virtio-blk-pci,drive=seed"]
    console = (work.parent / "logs" / "vm-console.log").open("w")
    try:
        process = subprocess.Popen([*argv, "-display", "none", "-serial", "stdio", "-monitor", "none", *network],
                                   stdin=subprocess.DEVNULL, stdout=console, stderr=subprocess.STDOUT)
    except BaseException:
        console.close()
        raise
    return process, port, console


class Guest:
    def __init__(self, port, key, logs, platform, candidate=None, fixture=None):
        self.port, self.key, self.logs = port, key, logs
        self.python = "/usr/libexec/platform-python" if platform == "rocky-amd64" else "python3"
        self.version = candidate.version if candidate else CURRENT
        self.candidate, self.fixture = candidate, fixture
        self.forward = None
        self.controller_environment = fixture.environment(fixture.cert) if fixture else {}
        self.guest_cert = str(fixture.cert) if fixture else ""
        self.guest_environment = fixture.environment(self.guest_cert) if fixture else {}
        self.roles = logs.parent / "roles"
        self.roles.mkdir(exist_ok=True)
        local_role = self.roles / "perimeterd"
        if not local_role.exists():
            local_role.symlink_to(ROLE, target_is_directory=True)
        self.inventory = logs.parent / "inventory.ini"
        interpreter = ("ansible_python_interpreter=/usr/libexec/platform-python "
                       if platform == "rocky-amd64" else "")
        self.inventory.write_text(
            "[perimeterd]\n"
            f"target ansible_host=127.0.0.1 ansible_port={port} ansible_user=ansible "
            f"ansible_ssh_private_key_file={key} {interpreter}"
            "ansible_ssh_common_args='-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null'\n")

    def ssh(self, command, *, timeout=60):
        return run(["ssh", *SSH_OPTIONS, "-p", str(self.port), "-i", str(self.key),
                    "ansible@127.0.0.1", command], timeout=timeout,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()

    def ready(self):
        deadline = time.monotonic() + 900
        last_error = None
        while time.monotonic() < deadline:
            try:
                self.ssh("cloud-init status --wait && sudo systemctl --version", timeout=90)
                print(f"disposable guest SSH ready on {self.port}", flush=True)
                return
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
                last_error = error
                time.sleep(4)
        raise TimeoutError(
            f"disposable VM did not complete cloud-init: {getattr(last_error, 'stderr', last_error)}"
        ) from last_error
    def connect_fixture(self):
        if not self.fixture:
            return
        self.ssh(f"mkdir -p {Path(self.guest_cert).parent} && chmod 700 {Path(self.guest_cert).parent}")
        # Trust only this process/play environment, never the global guest store.
        run(["scp", *SSH_OPTIONS, "-P", str(self.port), "-i", str(self.key),
             str(self.fixture.cert), f"ansible@127.0.0.1:{self.guest_cert}"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        log = (self.logs / "reverse-forward.log").open("w")
        try:
            self.forward = subprocess.Popen(
                ["ssh", *SSH_OPTIONS, "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=15",
                 "-p", str(self.port), "-i", str(self.key), "-N",
                 "-R", f"127.0.0.1:{self.fixture.port}:127.0.0.1:{self.fixture.port}",
                 "ansible@127.0.0.1"], stdout=log, stderr=subprocess.STDOUT)
        finally:
            log.close()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.forward.poll() is not None:
                raise RuntimeError("fixture SSH reverse forwarding failed; inspect reverse-forward.log")
            try:
                self.ssh(f"{self.python} -c 'import socket; socket.create_connection((\"127.0.0.1\", {self.fixture.port}), 2).close()'")
                return
            except subprocess.CalledProcessError:
                time.sleep(0.2)
        raise TimeoutError("fixture SSH reverse forwarding did not become ready")

    def close(self):
        if self.forward:
            self.forward.terminate()
            try:
                self.forward.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.forward.kill()
                self.forward.wait()
        if self.fixture:
            try:
                self.ssh(f"rm -f {self.guest_cert} && rmdir {Path(self.guest_cert).parent}")
            except (subprocess.SubprocessError, OSError):
                pass

    def play(self, label, *, version=None, config=None, state="started", enabled=True,
             check=False, fails=False, noop=False, diff=False, service=False, extra=None):
        variables = {"smoke_version": version or self.version, "smoke_config": config or BASE_CONFIG,
                     "smoke_service_state": state, "smoke_service_enabled": enabled,
                     "smoke_transport_environment": self.guest_environment}
        variables.update(extra or {})
        playbook = "service.yml" if service else ("check.yml" if check else "smoke.yml")
        argv = ["ansible-playbook", "-i", str(self.inventory), str(ROLE / "tests/vm" / playbook),
                "-e", json.dumps(variables)]
        if check:
            argv.append("--check")
        if diff:
            argv.append("--diff")
        env = dict(os.environ, ANSIBLE_NOCOLOR="1", ANSIBLE_HOST_KEY_CHECKING="False",
                   ANSIBLE_ROLES_PATH=str(self.roles), **self.controller_environment)
        try:
            completed = subprocess.run(argv, text=True, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, timeout=1000)
        except subprocess.TimeoutExpired as error:
            output = error.stdout or ""
            if isinstance(output, bytes):
                output = output.decode(errors="replace")
            (self.logs / f"{label}.log").write_text(output)
            raise
        (self.logs / f"{label}.log").write_text(completed.stdout)
        if (completed.returncode == 0) == fails:
            raise AssertionError(f"{label}: unexpected Ansible result {completed.returncode}; inspect {self.logs / (label + '.log')}")
        changed = re.search(r"(?m)^target\s+:.*?changed=(\d+)", completed.stdout)
        if not fails and changed is None:
            raise AssertionError(f"{label}: missing Ansible recap")
        if noop and int(changed.group(1)) != 0:
            raise AssertionError(f"{label}: expected zero changes, got {changed.group(1)}")
        if not fails and not service and self.version == CURRENT and variables["smoke_version"] == CURRENT:
            selected = re.search(r'RESOLVED_TAG=([^\s"]+)', completed.stdout)
            if selected is None:
                raise AssertionError(f"{label}: missing resolved prerelease tag")
            self.version = selected.group(1)
            print(f"Resolved published prerelease: {self.version}", flush=True)
        print(f"{label}: {'expected failure' if fails else 'pass'}" +
              (f", changed={changed.group(1)}" if changed else ""), flush=True)
        return completed.stdout


    def until(self, command, needle, *, timeout=45):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if needle in self.ssh(command):
                    return
            except subprocess.CalledProcessError:
                pass
            time.sleep(2)
        raise AssertionError(f"guest did not observe {needle!r} from {command!r}")

def assert_no_config_validation_claim(output, label):
    for line in output.splitlines():
        mentions_config = re.search(r"(?i)\b(?:config(?:uration)?|perimeterd_config)\b", line)
        claims_success = re.search(
            r"(?i)\b(?:validated|is valid|validation (?:passed|succeeded|successful))\b", line)
        disclaims_success = re.search(r"(?i)\b(?:not|never|deferred|skipped)\b", line)
        if mentions_config and claims_success and not disclaims_success:
            raise AssertionError(f"{label}: check mode claimed configuration validation")


def exercise(guest, platform):
    package_query = "dpkg-query -W -f='${Version}' perimeterd" if platform.startswith("debian") else "rpm -q perimeterd"
    guest.play("reject-branch-selector", version="main", fails=True)
    # A fresh --check is read-only even though no executable exists yet.
    fresh_check = guest.play("fresh-check", check=True, noop=True)
    assert_no_config_validation_claim(fresh_check, "fresh-check")
    guest.ssh(f"sudo test ! -e /etc/perimeterd/perimeterd.yaml && ! {package_query}")
    secret = "role-smoke-secret-must-not-appear-in-ansible-output"
    secret_config = {
        **BASE_CONFIG,
        "ip_lists": {"private-feed": {"url": f"https://invalid.example/list?token={secret}"}},
    }
    invalid_first = {**secret_config, "version": 2}
    invalid_check = guest.play(
        "invalid-config-check", config=invalid_first, check=True, noop=True, diff=True)
    assert secret not in invalid_check
    assert_no_config_validation_claim(invalid_check, "invalid-config-check")
    guest.ssh(f"sudo test ! -e /etc/perimeterd/perimeterd.yaml && ! {package_query}")

    assert secret not in guest.play("first-install-invalid", config=invalid_first, fails=True, diff=True)
    assert guest.ssh("sudo systemctl show --value -p ActiveState perimeterd.service") != "active"
    assert guest.ssh("sudo systemctl show --value -p UnitFileState perimeterd.service") != "enabled"
    assert_native_package(guest, platform, guest.version)
    guest.play("fresh-stopped", state="stopped", enabled=False)
    guest.play("stopped-noop", state="stopped", enabled=False, noop=True)
    # Changing configuration must not install another backend. These are caller
    # prerequisites, not role-managed packages; an unavailable backend may fail.
    iptables_tools = "sudo sh -c 'for tool in iptables ipset; do command -v \"$tool\" || true; done'"
    before_tools = guest.ssh(iptables_tools)
    unavailable_backend = {
        **BASE_CONFIG, "firewall": {"backend": "iptables"},
        "global": {"blocklist": ["8.8.8.8/32"]},
    }
    guest.play("configured-backend-does-not-install-tools", config=unavailable_backend,
               state="stopped", enabled=False)
    assert guest.ssh(iptables_tools) == before_tools, "configuration installed firewall tools"
    if len(before_tools.splitlines()) != 2:
        guest.play("unavailable-configured-backend-fails", config=unavailable_backend, fails=True)
        assert guest.ssh(iptables_tools) == before_tools, "failed start installed firewall tools"
    guest.play("restore-native-backend-config", state="stopped", enabled=False)
    # The lifecycle scenario below deliberately uses nftables; prepare it as the
    # operator, since native package dependencies may have selected iptables.
    if platform.startswith("debian"):
        guest.ssh("sudo apt-get install -y nftables", timeout=300)
    else:
        guest.ssh("sudo dnf install -y nftables", timeout=300)
    guest.play("start")
    first_pid = guest.ssh("sudo systemctl show --value -p MainPID perimeterd.service")
    guest.play("started-noop", noop=True)
    assert first_pid == guest.ssh("sudo systemctl show --value -p MainPID perimeterd.service")
    assert secret not in guest.play("secret-config-redacted", config=secret_config, diff=True)
    assert secret in guest.ssh("sudo cat /etc/perimeterd/perimeterd.yaml")
    guest.play("secret-config-noop", config=secret_config, noop=True)
    config_hash = guest.ssh("sudo sha256sum /etc/perimeterd/perimeterd.yaml")
    guest.play("disable-boot-only", config=secret_config, enabled=False)
    assert guest.ssh("sudo systemctl show --value -p UnitFileState perimeterd.service") not in (
        "enabled", "enabled-runtime"), "enablement-only change left the service enabled"
    assert config_hash == guest.ssh("sudo sha256sum /etc/perimeterd/perimeterd.yaml"), "enablement-only change modified config"
    assert first_pid == guest.ssh("sudo systemctl show --value -p MainPID perimeterd.service"), "enablement change restarted process"
    guest.play("enable-boot-only", config=secret_config)
    assert guest.ssh("sudo systemctl show --value -p UnitFileState perimeterd.service") in (
        "enabled", "enabled-runtime"), "enablement-only change did not enable the service"
    assert config_hash == guest.ssh("sudo sha256sum /etc/perimeterd/perimeterd.yaml"), "enablement-only change modified config"
    assert first_pid == guest.ssh("sudo systemctl show --value -p MainPID perimeterd.service"), "enablement change restarted process"
    converged_check = guest.play(
        "converged-check", config=secret_config, check=True, noop=True, diff=True)
    assert secret not in converged_check
    assert_no_config_validation_claim(converged_check, "converged-check")
    config = {**BASE_CONFIG, "global": {"blocklist": ["8.8.8.8/32"]}}
    guest.play("policy-reload", config=config)
    assert first_pid == guest.ssh("sudo systemctl show --value -p MainPID perimeterd.service"), "config-only change restarted service"
    guest.until("sudo nft list ruleset", "8.8.8.8")
    guest.play("policy-noop", config=config, noop=True)
    marker_hash = guest.ssh("sudo sha256sum /var/lib/perimeterd-ansible/config-applied")
    retry_config = {**BASE_CONFIG, "global": {"blocklist": ["8.8.8.8/32", "9.9.9.9/32"]}}
    # Referenced, uncached source is locally valid but unavailable at staging.
    rejected = runtime_rejected_config(retry_config)
    before_state = selected_state(guest)
    output = guest.play("runtime-rejected-reload", config=rejected, fails=True)
    assert "configuration_error" in output
    assert marker_hash == guest.ssh("sudo sha256sum /var/lib/perimeterd-ansible/config-applied")
    assert selected_state(guest) == before_state, "runtime rejection changed selected policy"
    assert first_pid == guest.ssh("sudo systemctl show --value -p MainPID perimeterd.service")
    assert guest.ssh("sudo systemctl is-active perimeterd.service") == "active"
    expect_command_failure(guest, "systemctl-runtime-rejected", "sudo systemctl reload perimeterd")
    assert selected_state(guest) == before_state
    assert first_pid == guest.ssh("sudo systemctl show --value -p MainPID perimeterd.service")
    config = retry_config
    guest.play("retry-failed-reload", config=config)
    guest.until("sudo nft list ruleset", "9.9.9.9")
    guest.play("retry-noop", config=config, noop=True)
    acknowledgement(guest, config, "cli-applied")
    service_failures(guest, config)
    first_pid = guest.ssh("sudo systemctl show --value -p MainPID perimeterd.service")
    previous_hash = guest.ssh("sudo sha256sum /etc/perimeterd/perimeterd.yaml")
    invalid = {**config, "version": 2}
    guest.play("invalid-config", config=invalid, fails=True)
    assert previous_hash == guest.ssh("sudo sha256sum /etc/perimeterd/perimeterd.yaml")
    assert first_pid == guest.ssh("sudo systemctl show --value -p MainPID perimeterd.service")
    guest.ssh("sudo nft add table inet role_smoke_foreign")
    try:
        guest.ssh("sudo modinfo -F filename ip_tables && sudo modinfo -F filename ip6_tables")
    except subprocess.CalledProcessError:
        print("iptables-legacy IPv4/IPv6 kernel modules unavailable; exercising nftables-only coexistence", flush=True)
    else:
        # The operator prepares the new backend before changing configuration.
        if platform.startswith("debian"):
            guest.ssh("sudo apt-get update -qq && sudo apt-get install -y iptables ipset", timeout=300)
        else:
            guest.ssh("sudo dnf install -y iptables ipset", timeout=300)
        guest.ssh("sudo modprobe ip_tables && sudo modprobe ip6_tables")
        iptables_config = {**config, "firewall": {"backend": "iptables"}}
        guest.play("backend-iptables", config=iptables_config)
        guest.until("sudo iptables-save", "perimeterd")
        guest.ssh("sudo test -d /sys/module/ip_tables -a -d /sys/module/ip6_tables")
        assert guest.ssh("sudo nft list table inet role_smoke_foreign"), "foreign firewall table was removed"
        guest.play("backend-iptables-noop", config=iptables_config, noop=True)
    guest.play("backend-nftables", config=config)
    guest.until("sudo nft list ruleset", "8.8.8.8")
    assert guest.ssh("sudo nft list table inet role_smoke_foreign"), "foreign firewall table was removed"
    current_format_lifecycle(guest, platform, config)


def runtime_rejected_config(config):
    return {**config,
            "ip_lists": {"reload-unavailable": {
                "url": "http://127.0.0.1:9/reload-unavailable", "request_timeout": "1s"}},
            "policies": [{"name": "reload-source-failure", "priority": 100,
                          "direction": "ingress", "mode": "blocklist", "traffic": ["any"],
                          "include": {"ip_lists": ["reload-unavailable"]}}]}


def expect_command_failure(guest, label, command, *, timeout=100):
    try:
        guest.ssh(command, timeout=timeout)
    except subprocess.CalledProcessError as error:
        (guest.logs / f"{label}.log").write_text(
            f"exit={error.returncode}\n{error.stdout or ''}\n{error.stderr or ''}")
        return error
    raise AssertionError(f"{label}: command unexpectedly succeeded")


def acknowledgement(guest, config, label):
    digest = guest.ssh("sudo sha256sum /etc/perimeterd/perimeterd.yaml").split()[0]
    output = guest.ssh(f"sudo /usr/bin/perimeterd reload --expect-config-sha256 {digest}", timeout=90)
    state = require_selected(guest, config)
    assert digest in output, "acknowledgement omitted actual configuration digest"
    assert state["revision"]["id"] in output, "acknowledgement omitted selected revision"
    (guest.logs / f"{label}.log").write_text(output + "\n")
    marker = guest.ssh("sudo cat /var/lib/perimeterd-ansible/config-applied")
    assert f"config_sha256={digest}" in marker
    return state


def service_failures(guest, config):
    import base64
    original = guest_python(guest, "import base64; print(base64.b64encode(open('/etc/perimeterd/perimeterd.yaml','rb').read()).decode())")
    marker = guest_python(guest, "import base64; print(base64.b64encode(open('/var/lib/perimeterd-ansible/config-applied','rb').read()).decode())")
    identity = base64.b64decode(marker).decode().split("package=", 1)[1].strip()
    inputs = {"smoke_package_identity": identity}
    pid = guest.ssh("sudo systemctl show --value -p MainPID perimeterd.service")
    state = selected_state(guest)
    marker_path = "/var/lib/perimeterd-ansible/config-applied"

    def restore():
        guest_python(guest, "import base64; "
                     f"open('/etc/perimeterd/perimeterd.yaml','wb').write(base64.b64decode({original!r})); "
                     f"open({marker_path!r},'wb').write(base64.b64decode({marker!r}))")

    def unchanged(expected):
        actual = guest_python(guest, "from pathlib import Path; "
                              f"p=Path({marker_path!r}); print(p.read_bytes().hex() if p.exists() else 'absent')")
        assert actual == expected, "failed/unconfirmed reload changed marker bytes or absence"
        assert selected_state(guest) == state, "pre-apply failure changed the selected policy"
        assert guest.ssh("sudo systemctl show --value -p MainPID perimeterd.service") == pid
        assert guest.ssh("sudo systemctl is-active perimeterd.service") == "active"

    sentinel = b"previous-marker\x00\r\n"
    try:
        guest_python(guest, f"open({marker_path!r},'wb').write({sentinel!r})")
        guest.play("service-check-reload", service=True, check=True, extra=inputs)
        unchanged(sentinel.hex())
        replacement = {**config, "global": {"blocklist": ["192.0.2.99/32"]}}
        output = guest.play("render-read-digest-mismatch", service=True, fails=True,
                            extra={**inputs, "smoke_replacement_config": replacement})
        assert "config_mismatch" in output
        unchanged(sentinel.hex())
        restore()
        guest.ssh(f"sudo rm -f {marker_path}")
        output = guest.play("render-read-digest-mismatch-absent", service=True, fails=True,
                            extra={**inputs, "smoke_replacement_config": replacement})
        assert "config_mismatch" in output
        unchanged("absent")
        restore()
        guest.ssh(f"sudo rm -f {marker_path}")
        output = guest.play("runtime-rejected-marker-absent",
                            config=runtime_rejected_config(config), fails=True)
        assert "configuration_error" in output
        unchanged("absent")
        restore()
        for mode in ("unavailable", "old-endpoint", "malformed-response", "timeout"):
            for absent in (False, True):
                if absent:
                    guest.ssh(f"sudo rm -f {marker_path}")
                    expected = "absent"
                else:
                    guest_python(guest, f"open({marker_path!r},'wb').write({sentinel!r})")
                    expected = sentinel.hex()
                with PrivateSocketFailure(guest, mode):
                    started = time.monotonic()
                    output = guest.play(f"{mode}-marker-{'absent' if absent else 'preserved'}",
                                        service=True, fails=True, extra=inputs)
                    assert "unknown" in output.lower()
                    if mode == "timeout":
                        assert time.monotonic() - started >= 70, "timeout did not exercise the real CLI wait bound"
                        assert "unknown" in output.lower(), "timeout omitted unknown-completion diagnostic"
                unchanged(expected)
        restore()
        guest.play("service-package-change-restarts", service=True,
                   extra={**inputs, "smoke_package_changed": True})
        assert guest.ssh("sudo systemctl show --value -p MainPID perimeterd.service") != pid
        guest.play("service-package-change-noop", service=True, extra=inputs, noop=True)
    finally:
        restore()


class PrivateSocketFailure:
    """Real Unix transport faults; never replace the CLI or daemon executable."""

    def __init__(self, guest, mode):
        self.guest, self.mode = guest, mode

    def __enter__(self):
        guest = self.guest
        guest.ssh("sudo mv /run/perimeterd/lookup.sock /run/perimeterd/lookup.sock.saved")
        if self.mode == "unavailable":
            return self
        code = """import os, socket, time
s = socket.socket(socket.AF_UNIX)
s.bind('/run/perimeterd/lookup.sock')
os.chmod('/run/perimeterd/lookup.sock', 0o600)
s.listen(2)
open('/run/perimeterd/socket-fault-ready','w').close()
c, _ = s.accept()
c.recv(4096)
"""
        if self.mode == "timeout":
            code += "time.sleep(85)\n"
        else:
            status, body = ("404 Not Found", b"old daemon endpoint") if self.mode == "old-endpoint" else ("200 OK", b"not-json")
            response = f"HTTP/1.1 {status}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body
            code += f"c.sendall({response!r})\nc.close()\n"
        try:
            guest.ssh("sudo systemd-run --collect --unit=role-socket-fault " + guest.python + " -c " + shlex.quote(code))
            guest.until("sudo test -f /run/perimeterd/socket-fault-ready && echo ready", "ready", timeout=15)
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *_):
        self.guest.ssh("sudo systemctl stop role-socket-fault.service 2>/dev/null || true; "
                       "sudo systemctl reset-failed role-socket-fault.service 2>/dev/null || true; "
                       "sudo rm -f /run/perimeterd/lookup.sock /run/perimeterd/socket-fault-ready; "
                       "sudo mv /run/perimeterd/lookup.sock.saved /run/perimeterd/lookup.sock")


def guest_python(guest, code):
    return guest.ssh("sudo " + guest.python + " -c " + shlex.quote(code))


def assert_native_package(guest, platform, version):
    native = re.sub(r"^([0-9]+\.[0-9]+\.[0-9]+)-", r"\1~", version.removeprefix("v")) + "-1"
    if platform.startswith("debian"):
        actual = guest.ssh("dpkg-query -W -f='${Version}:${Architecture}' perimeterd")
        expected = native + ":amd64"
    else:
        actual = guest.ssh("rpm -q --qf '%{EPOCHNUM}:%{VERSION}-%{RELEASE}:%{ARCH}' perimeterd")
        expected = "0:" + native + (":aarch64" if platform.endswith("arm64") else ":x86_64")
    assert actual == expected, f"actual package {actual!r} differs from selected {expected!r}"
    with (guest.logs / "native-packages.log").open("a") as output:
        output.write(f"perimeterd selector={version} installed={actual}\n")


def selected_state(guest):
    code = """import json
from pathlib import Path
p = Path('/var/lib/perimeterd')
def payload(path):
    record = json.loads(path.read_text())
    assert record['version'] == 2, 'current durable record envelope must be version 2'
    return record['payload']
owner = payload(p/'owner.json')
active = payload(p/'active.json')
revision = payload(p/'revisions'/(active['id']+'.json'))
assert revision['version'] == 2, 'current revision must be version 2'
print(json.dumps({'owner': owner, 'revision': revision, 'pending': (p/'journal.json').exists()}))
"""
    return json.loads(guest_python(guest, code))


def require_selected(guest, config):
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        state = selected_state(guest)
        revision = state["revision"]
        target = revision["target"]
        blocked = revision["config"]["Global"]["Blocklist"] or []
        if not state["pending"] and blocked == config.get("global", {}).get("blocklist", []):
            assert revision["config"]["Firewall"]["Backend"] == "nftables"
            assert target is not None
            (guest.logs / "selected-policy.json").write_text(json.dumps(state, indent=2))
            return state
        time.sleep(0.2)
    (guest.logs / "unselected-policy.json").write_text(json.dumps(state, indent=2))
    raise AssertionError("requested policy did not become the durable selected current-format policy")




def packet_inventory(guest, label, state):
    inventory = json.loads(guest.ssh("sudo nft -j list ruleset"))
    (guest.logs / f"packet-{label}-native.json").write_text(json.dumps(inventory, indent=2))
    (guest.logs / f"packet-{label}-selected.json").write_text(json.dumps(state, indent=2))
    target = state["revision"]["target"]
    owner, table = target["owner"], target["table"]
    chain_name = f"pd_{owner}_stable_ownership"
    bound = lambda obj: obj.get("family") == "inet" and obj.get("table") == table
    chains = [item["chain"] for item in inventory["nftables"] if "chain" in item
              and bound(item["chain"]) and item["chain"].get("name") == chain_name]
    rules = [item["rule"] for item in inventory["nftables"] if "rule" in item
             and bound(item["rule"]) and item["rule"].get("chain") == chain_name]
    assert len(chains) == 1 and len(rules) == 1, "canonical stable witness missing/duplicated"
    assert all(type(obj.get("handle")) is int and obj["handle"] > 0 for obj in (chains[0], rules[0])), "stable witness must expose native handles"
    assert not any(key in chains[0] for key in ("type", "hook", "prio", "policy", "flags", "timeout"))
    assert rules[0]["comment"] == f"perimeterd owner={owner} role=table_witness_v1"
    assert rules[0]["expr"] == [{"return": None}]
    specs = [counter for counter in target["counters"] if counter["family"] == 4
             and counter["direction"] == "ingress"
             and counter["role"] == {"Kind": "denied", "Reason": "global_blocklist", "Action": "drop"}]
    assert len(specs) == 1, "selected target must name one IPv4 ingress global-blocklist drop counter"
    name = specs[0]["name"]
    assert name.endswith("_denied_global_blocklist_drop")
    counters = [item["counter"] for item in inventory["nftables"] if "counter" in item
                and bound(item["counter"]) and item["counter"].get("name") == name]
    assert len(counters) == 1, "selected named ingress drop counter is missing/duplicated in native inventory"
    assert type(counters[0]["packets"]) is int and type(counters[0]["bytes"]) is int
    return (chains[0], rules[0]), counters[0]


def prove_enforcement(guest, config):
    # Real inbound packets from an isolated namespace; no Internet reachability.
    allowed = {**config, "global": {"blocklist": ["9.9.9.9/32"]}}
    guest.play("packet-allow-policy", config=allowed)
    allowed_state = require_selected(guest, allowed)
    try:
        guest.ssh("sudo ip netns add role-packets && sudo ip link add role-host type veth peer name role-peer && "
                  "sudo ip link set role-peer netns role-packets && sudo ip addr add 192.0.2.1/32 dev role-host && "
                  "sudo ip link set role-host up && sudo ip route add 8.8.8.8/32 dev role-host && "
                  "sudo ip netns exec role-packets ip addr add 8.8.8.8/32 dev role-peer && "
                  "sudo ip netns exec role-packets ip link set role-peer up && "
                  "sudo ip netns exec role-packets ip route add 192.0.2.1/32 dev role-peer")
        guest.ssh("sudo ip netns exec role-packets ping -c 1 -W 3 192.0.2.1")
        allow_witness, allow_counter = packet_inventory(guest, "allow", allowed_state)
        guest.play("packet-deny-policy", config=config)
        expect_command_failure(guest, "packet-role-return-denied",
                               "sudo ip netns exec role-packets ping -c 1 -W 3 192.0.2.1")
        denied_state = require_selected(guest, config)
        acknowledgement(guest, config, "packet-role-acknowledged")
        denied_state = require_selected(guest, config)
        deny_witness, before_counter = packet_inventory(guest, "deny-before", denied_state)
        assert deny_witness == allow_witness, "policy reload replaced the stable witness chain/rule identity"
        assert before_counter["name"] == allow_counter["name"], "policy reload changed stable accounting identity"
        try:
            guest.ssh("sudo ip netns exec role-packets ping -c 1 -W 3 192.0.2.1")
        except subprocess.CalledProcessError:
            pass
        else:
            raise AssertionError("selected policy did not block real native packets")
        after_witness, after_counter = packet_inventory(guest, "deny-after", denied_state)
        assert after_witness == deny_witness, "packet traversal changed the unhooked ownership witness"
        assert after_counter["packets"] > before_counter["packets"], "denied packet did not increment the selected ingress drop counter"
        assert after_counter["bytes"] > before_counter["bytes"], "denied packet bytes were not accounted by the selected ingress drop counter"
        (guest.logs / "packet-counter-delta.json").write_text(json.dumps({
            "name": after_counter["name"], "packets": after_counter["packets"] - before_counter["packets"],
            "bytes": after_counter["bytes"] - before_counter["bytes"]}, indent=2))
        packet_reload_contracts(guest, config, allowed)
        guest.play("packet-policy-noop", config=config, noop=True)
    finally:
        guest.ssh("sudo ip link del role-host 2>/dev/null || true; sudo ip netns del role-packets 2>/dev/null || true")


def packet_reload_contracts(guest, denied, allowed):
    ping = "sudo ip netns exec role-packets ping -c 1 -W 3 192.0.2.1"
    pid = guest.ssh("sudo systemctl show --value -p MainPID perimeterd.service")
    state = selected_state(guest)
    marker = guest_python(guest, "print(open('/var/lib/perimeterd-ansible/config-applied','rb').read().hex())")

    def write(config):
        guest_python(guest, f"open('/etc/perimeterd/perimeterd.yaml','w').write({json.dumps(config)!r})")

    def old_policy(label):
        assert selected_state(guest) == state
        assert guest.ssh("sudo systemctl show --value -p MainPID perimeterd.service") == pid
        assert guest.ssh("sudo systemctl is-active perimeterd.service") == "active"
        assert guest_python(guest, "print(open('/var/lib/perimeterd-ansible/config-applied','rb').read().hex())") == marker
        expect_command_failure(guest, label + "-packet-denied", ping)

    rejected = runtime_rejected_config(allowed)
    output = guest.play("packet-runtime-rejected-role", config=rejected, fails=True)
    assert "configuration_error" in output
    validated = guest.ssh("sudo /usr/bin/perimeterd validate --config /etc/perimeterd/perimeterd.yaml")
    (guest.logs / "packet-runtime-rejected-validated.log").write_text("exit=0\n" + validated)
    failure = expect_command_failure(guest, "packet-runtime-rejected-cli", "sudo /usr/bin/perimeterd reload")
    assert "rejected" in failure.stderr
    old_policy("runtime-cli")
    expect_command_failure(guest, "packet-runtime-rejected-systemctl", "sudo systemctl reload perimeterd")
    old_policy("runtime-systemctl")
    write(allowed)
    digest = guest.ssh("sudo sha256sum /etc/perimeterd/perimeterd.yaml").split()[0]
    mismatch = ("0" if digest[0] != "0" else "1") + digest[1:]
    failure = expect_command_failure(guest, "packet-digest-mismatch",
                                    f"sudo /usr/bin/perimeterd reload --expect-config-sha256 {mismatch}")
    assert "config_mismatch" in failure.stderr
    old_policy("digest-mismatch")
    # No polling before packets: systemctl success itself must establish apply.
    output = guest.ssh("sudo systemctl reload perimeterd", timeout=90)
    guest.ssh(ping)
    applied = require_selected(guest, allowed)
    digest = guest.ssh("sudo sha256sum /etc/perimeterd/perimeterd.yaml").split()[0]
    (guest.logs / "packet-systemctl-valid.log").write_text(
        f"exit=0\nrevision={applied['revision']['id']}\nconfig_sha256={digest}\n" + output)
    assert guest.ssh("sudo systemctl show --value -p MainPID perimeterd.service") == pid
    write(denied)
    guest.ssh("sudo systemctl reload perimeterd", timeout=90)
    expect_command_failure(guest, "systemctl-valid-denied", ping)
    require_selected(guest, denied)
    # Retain manual asynchronous SIGHUP coverage, with explicit convergence.
    for label, config in (("manual-allow", allowed), ("manual-deny", denied)):
        write(config)
        guest.ssh("sudo systemctl kill --kill-who=main --signal=HUP perimeterd")
        require_selected(guest, config)
        if label == "manual-allow":
            guest.ssh(ping)
        else:
            expect_command_failure(guest, label, ping)
    guest.play("packet-contracts-restore-marker", config=denied)
    acknowledgement(guest, denied, "packet-contracts-final-ack")
    assert guest.ssh("sudo systemctl show --value -p MainPID perimeterd.service") == pid


def stopped_retention(guest, platform, config):
    guest.ssh("sudo sh -c 'printf retained > /var/lib/perimeterd/vm-smoke-state'")
    retained_state = selected_state(guest)
    guest.play("stopped-config-change", config=BASE_CONFIG, state="stopped", enabled=False)
    assert_native_package(guest, platform, guest.version)
    assert selected_state(guest) == retained_state, "stopping or changing stopped config mutated selected state"
    assert guest.ssh("sudo cat /var/lib/perimeterd/vm-smoke-state") == "retained"
    guest.play("stopped-current-policy", config=config, state="stopped", enabled=False)
    assert selected_state(guest) == retained_state
    guest.play("stopped-current-noop", config=config, state="stopped", enabled=False, noop=True)
    assert guest.ssh("sudo systemctl show --value -p ActiveState perimeterd.service") == "inactive"
    assert guest.ssh("sudo cat /var/lib/perimeterd/vm-smoke-state") == "retained"
    guest.play("restart-after-stop", config=config)
    require_selected(guest, config)
    assert guest.ssh("sudo cat /var/lib/perimeterd/vm-smoke-state") == "retained"


def current_format_lifecycle(guest, platform, config):
    assert_native_package(guest, platform, guest.version)
    prove_enforcement(guest, config)
    guest.play("current-format-noop", config=config, noop=True)
    stopped_retention(guest, platform, config)
    print(f"current-format service/enforcement lifecycle PASS: {platform}", flush=True)


def fresh_current_lifecycle(guest, platform):
    guest.play("fresh-current-stopped", state="stopped", enabled=False)
    assert_native_package(guest, platform, guest.version)
    guest.ssh("sudo test ! -e /var/lib/perimeterd/owner.json && sudo test ! -e /var/lib/perimeterd/active.json")
    assert "perimeterd" not in guest.ssh("sudo nft list ruleset"), "fresh stopped package established native policy"
    guest.play("fresh-current-empty-start")
    state = selected_state(guest)
    assert state["revision"]["target"] is None
    assert "perimeterd" not in guest.ssh("sudo nft list ruleset")
    running_pid = guest.ssh("sudo systemctl show --value -p MainPID perimeterd.service")
    assert int(running_pid) > 0
    config_hash = guest.ssh("sudo sha256sum /etc/perimeterd/perimeterd.yaml")
    running_inode = guest.ssh(f"sudo stat -Lc '%d:%i' /proc/{running_pid}/exe")
    # Test-only atomic replacement of the actual installed bytes. No native
    # package is fabricated, renamed, relabelled, or substituted.
    guest.ssh("sudo sh -c 'set -eu; replacement=$(mktemp /usr/bin/.perimeterd-vm.XXXXXX); "
              "trap \"rm -f $replacement\" EXIT; cp --preserve=all /usr/bin/perimeterd \"$replacement\"; "
              "cmp /usr/bin/perimeterd \"$replacement\"; mv -f \"$replacement\" /usr/bin/perimeterd'")
    installed_inode = guest.ssh("stat -Lc '%d:%i' /usr/bin/perimeterd")
    assert running_inode != installed_inode, "atomic byte-identical replacement did not stale the running inode"
    assert running_inode == guest.ssh(f"sudo stat -Lc '%d:%i' /proc/{running_pid}/exe")
    assert_native_package(guest, platform, guest.version)
    check = guest.play("fresh-stale-inode-check", check=True, noop=True)
    assert_no_config_validation_claim(check, "fresh-stale-inode-check")
    assert running_pid == guest.ssh("sudo systemctl show --value -p MainPID perimeterd.service")
    assert config_hash == guest.ssh("sudo sha256sum /etc/perimeterd/perimeterd.yaml")
    config = {**BASE_CONFIG, "global": {"blocklist": ["8.8.8.8/32"]}}
    guest.play("fresh-stale-inode-invalid-config", config={**config, "version": 2}, fails=True)
    assert_native_package(guest, platform, guest.version)
    assert running_pid == guest.ssh("sudo systemctl show --value -p MainPID perimeterd.service")
    assert config_hash == guest.ssh("sudo sha256sum /etc/perimeterd/perimeterd.yaml")
    assert running_inode == guest.ssh(f"sudo stat -Lc '%d:%i' /proc/{running_pid}/exe")
    assert installed_inode == guest.ssh("stat -Lc '%d:%i' /usr/bin/perimeterd")
    guest.play("fresh-recover-stale-inode", config=config)
    current_pid = guest.ssh("sudo systemctl show --value -p MainPID perimeterd.service")
    assert running_pid != current_pid, "valid role rerun did not restart stale process"
    assert installed_inode == guest.ssh(f"sudo stat -Lc '%d:%i' /proc/{current_pid}/exe")
    require_selected(guest, config)
    guest.play("fresh-stale-recovery-noop", config=config, noop=True)
    prove_enforcement(guest, config)
    stopped_retention(guest, platform, config)
    print(f"fresh current-format stale-inode lifecycle PASS: {platform}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", required=True, choices=sorted(IMAGES))
    parser.add_argument("--workdir", type=Path)
    parser.add_argument("--image-cache", type=Path)
    parser.add_argument("--external-port", type=int, help="reuse an already isolated/booted disposable VM (published mode only)")
    parser.add_argument("--identity", type=Path, help="SSH key for --external-port")
    parser.add_argument("--candidate-dist", type=Path, help="original GoReleaser dist with metadata.json, checksums.txt and native packages")
    args = parser.parse_args()
    if args.candidate_dist and args.external_port:
        parser.error("--candidate-dist requires two runner-owned disposable overlays; --external-port is forbidden")
    if args.external_port and args.identity is None:
        parser.error("--identity is required with --external-port")
    candidate = Candidate(args.candidate_dist, args.platform) if args.candidate_dist else None
    print(f"MODE: {'local candidate ' + candidate.version + ' (two owned overlays)' if candidate else 'published release ' + CURRENT}", flush=True)
    workdir = (args.workdir or Path(tempfile.mkdtemp(prefix="perimeterd-role-vm-"))).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    logs = workdir / "logs"
    logs.mkdir(exist_ok=True)
    if args.external_port:
        exercise(Guest(args.external_port, args.identity, logs, args.platform), args.platform)
        return
    image = IMAGES[args.platform]
    cache = (args.image_cache or workdir / "images").resolve()
    disk = fetch_image(image, cache)
    with ExitStack() as stack:
        fixture = None
        if candidate:
            print("Transport: synthetic candidate release metadata + unaltered local package/manifest at original trusted TLS URLs", flush=True)
            fixture = PackageFixture(candidate, logs)
            stack.callback(fixture.close)
        secrets = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="vm-keys-", dir=workdir)))
        key = secrets / "ssh-key"
        run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)])
        published_version = CURRENT
        scenarios = (("current-format", exercise), ("fresh-stale-inode", fresh_current_lifecycle))
        for scenario, execute in scenarios:
            directory = workdir / scenario
            scenario_logs = directory / "logs"
            scenario_logs.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="vm-overlay-", dir=directory) as overlay:
                process, port, console = boot(image, disk, Path(overlay), key)
                guest = None
                try:
                    guest = Guest(port, key, scenario_logs, args.platform, candidate, fixture)
                    if not candidate:
                        guest.version = published_version
                    guest.ready()
                    # Operator prerequisites before any restricted fixture environment.
                    command = ("sudo apt-get update -qq && sudo apt-get install -y nftables iproute2 iputils-ping ca-certificates xz-utils"
                               if args.platform.startswith("debian") else
                               "sudo dnf install -y nftables iproute iputils ca-certificates && sudo dnf makecache")
                    guest.ssh(command, timeout=600)
                    stack_query = ("uname -r; nft --version; dpkg-query -W nftables libnftnl11; apt-get --version"
                                   if args.platform.startswith("debian") else
                                   "uname -r; nft --version; rpm -q nftables libnftnl; dnf --version")
                    (scenario_logs / "guest-stack.log").write_text(guest.ssh(stack_query))
                    guest.connect_fixture()
                    execute(guest, args.platform)
                    if not candidate:
                        published_version = guest.version
                except BaseException:
                    if guest:
                        for label, command in (
                            ("daemon-journal", "sudo journalctl -u perimeterd.service -n 150 --no-pager"),
                            ("nft-rules", "sudo nft -j list ruleset"),
                            ("iptables-rules", "sudo iptables-save"),
                            ("ipsets", "sudo ipset list"),
                            ("native-package", "dpkg-query -W perimeterd 2>/dev/null || rpm -qi perimeterd"),
                            ("service", "sudo systemctl status perimeterd.service --no-pager"),
                        ):
                            try:
                                (scenario_logs / f"guest-{label}.log").write_text(guest.ssh(command, timeout=30))
                            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
                                output = "\\n".join(
                                    part.decode(errors="replace") if isinstance(part, bytes) else part
                                    for part in (error.stdout, error.stderr) if part)
                                (scenario_logs / f"guest-{label}.log").write_text(output)
                    raise
                finally:
                    if guest:
                        guest.close()
                    process.terminate()
                    try:
                        process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                    console.close()


if __name__ == "__main__":
    main()
