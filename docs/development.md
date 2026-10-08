# Development

Run these commands from the role checkout unless stated otherwise. The [README](../README.md) covers getting started and the [operator reference](reference.md) covers runtime contracts; this guide covers maintainer environments and test procedures, not a record of past results.

## Controller fixtures, syntax and lint

Install Python 3.14, Git, OpenPGP tools (`gpg` and `gpgconf`) and OpenSSL first. Release fixtures use disposable signed Git repositories; without Git/GPG those admission tests are skipped. Resolver fixtures use a local TLS proxy and real Ansible tasks at the role's fixed GitHub URLs. They do not establish hosted API health or package availability.

Create the modern development environment:

```sh
python3.14 -m venv .venv
. .venv/bin/activate
python -m pip install 'ansible-core==2.21.*' 'ansible-lint==26.9.0'
export ANSIBLE_COLLECTIONS_PATH="$VIRTUAL_ENV/collections"
ansible-galaxy collection install -r requirements-collections.yml
mkdir -p "$VIRTUAL_ENV/roles"
ln -s "$PWD" "$VIRTUAL_ENV/roles/perimeterd"
export ANSIBLE_ROLES_PATH="$VIRTUAL_ENV/roles"
python -m unittest discover -s tests -p 'test_*.py'
ansible-playbook -i 'perimeterd,' -c local --syntax-check tests/vm/smoke.yml
ansible-lint --strict .
```

The role symlink makes the actual named role resolvable by the smoke play. Create it once per environment. Run the same fixture discovery and syntax check in the isolated legacy environment:

```sh
deactivate
python3.12 -m venv .venv-legacy
. .venv-legacy/bin/activate
python -m pip install 'ansible-core==2.16.*'
export ANSIBLE_COLLECTIONS_PATH="$VIRTUAL_ENV/collections"
ansible-galaxy collection install -r requirements-collections-legacy.yml
mkdir -p "$VIRTUAL_ENV/roles"
ln -s "$PWD" "$VIRTUAL_ENV/roles/perimeterd"
export ANSIBLE_ROLES_PATH="$VIRTUAL_ENV/roles"
python -m unittest discover -s tests -p 'test_*.py'
ansible-playbook -i 'perimeterd,' -c local --syntax-check tests/vm/smoke.yml
```

A virtual environment alone does not isolate Ansible collections. Reset `ANSIBLE_COLLECTIONS_PATH` and `ANSIBLE_ROLES_PATH` when switching environments. Modern requirements leave `community.general` uncapped; only legacy core 2.16 uses the separate below-12 collection range. Keep lint in the 2.21 environment so installing the linter cannot upgrade the legacy controller.

## Disposable VM profiles

The [CI workflow](../.github/workflows/ci.yml) has distinct controller-fixture and VM matrices. Use the VM's matching controller profile, not merely the default development environment:

| Harness platform | Guest / package manager | Controller Python | ansible-core | Collections |
| --- | --- | --- | --- | --- |
| `debian-amd64` | Debian 13 / APT | 3.13 | latest 2.20 patch | modern |
| `fedora-arm64` | Fedora 44 / DNF5 | 3.14 | latest 2.21 patch | modern |
| `rocky-amd64` | Rocky Linux 8 / DNF4 | 3.12 | latest 2.16 patch | legacy |

**Debian's core 2.20 / Python 3.13 pairing is intentional.** Controller fixtures cover 2.16 and 2.21, while the Debian VM supplies a separate 2.20 compatibility gate. Do not flatten the matrices when updating documentation or dependencies. Controller Python is distinct from guest Python; the Rocky harness uses `/usr/libexec/platform-python`.

For Debian, create its separate controller environment:

```sh
deactivate
python3.13 -m venv .venv-debian
. .venv-debian/bin/activate
python -m pip install 'ansible-core==2.20.*'
export ANSIBLE_COLLECTIONS_PATH="$VIRTUAL_ENV/collections"
ansible-galaxy collection install -r requirements-collections.yml
```

The VM runner configures named-role lookup itself. Activate the corresponding environment and collection path before each command below:

```sh
# Debian: Python 3.13 / core 2.20 / modern collections
python tests/vm/run.py --platform debian-amd64 --require-kvm \
  --workdir "${HOME}/perimeterd-role-vm-debian-amd64"
# Fedora: Python 3.14 / core 2.21 / modern collections
python tests/vm/run.py --platform fedora-arm64 \
  --workdir "${HOME}/perimeterd-role-vm-fedora-arm64"
# Rocky: Python 3.12 / core 2.16 / isolated legacy collections
python tests/vm/run.py --platform rocky-amd64 --require-kvm \
  --workdir "${HOME}/perimeterd-role-vm-rocky-amd64"
```

The host needs QEMU (`qemu-system-x86_64` and/or `qemu-system-aarch64`), `qemu-img`, `genisoimage`, `ssh-keygen`, `ssh` and OpenSSL. ARM64 also needs firmware at one of the paths supported by the [runner](../tests/vm/run.py), provided by `qemu-efi-aarch64` or `edk2-aarch64`. The Ubuntu CI setup installs:

```sh
sudo apt-get update
sudo apt-get install --yes qemu-system-x86 qemu-system-arm qemu-efi-aarch64 qemu-utils genisoimage openssh-client
```

Allow outbound HTTPS for cloud images, official GitHub artifacts and guest package repositories. Downloads can consume several GB; each guest has 2 GiB RAM and a 10 GiB overlay. An optional `PERIMETERD_TEST_GITHUB_TOKEN` supplies the smoke play's GitHub API authentication; never commit a token.

Local runs without `--require-kvm` automatically select KVM only for a native guest architecture with readable/writable `/dev/kvm`, otherwise TCG. Host aliases `x86_64`/`amd64` and `aarch64`/`arm64` are equivalent. `--require-kvm` requires both prerequisites before any image download or overlay creation and requests KVM only: failed initialization is an error, never a fallback to TCG. Device access alone does not prove acceleration works. The runner reports host/guest architecture and the selected accelerator before boot, then reports successful guest readiness separately. An owned QEMU process exiting during readiness fails promptly with the console-log path.

Hosted Debian and Rocky jobs require KVM and grant device access only to the current disposable runner user; a missing KVM character device or broken virtualization fails the job. Fedora ARM64 remains on the hosted x86-64 runner with TCG. Cross-architecture emulation can take hours; the three controller profiles and both scenarios are unchanged.

Every harness-launched Ansible play uses SSH pipelining via its subprocess-local `ANSIBLE_PIPELINING=True` environment, including check/diff, service-only, expected-failure and no-op calls. No repository/global Ansible configuration or production role default is changed. Cloud-init retains the passwordless-sudo test user; direct SSH/SCP, guest interpreters and file-transfer tasks retain their existing behavior.

Use a separate `--workdir` per platform/run outside the role checkout. It retains image caches and diagnostics, including top-level `logs/` and scenario-specific `current-format/logs/` and `fresh-stale-inode/logs/`. Temporary SSH keys and overlays are removed and owned QEMU processes are terminated on exit. Without `--workdir`, the runner creates a temporary working directory; `--image-cache` can designate a shared image cache. Image URLs and checksums remain authoritative in the harness, not copied into this guide.

Both owned-overlay modes exercise real native packages, systemd and kernel firewall behavior in isolated user-mode networking. Published mode starts with `latest-prerelease`, records the first concrete resolved daemon tag, and reuses it across subsequent role calls and both overlays. It is not a stable-release selector. A standalone smoke play can select the latest stable daemon with `-e smoke_version=latest`, or an exact published tag; run it only against a disposable guest.

### Locally built daemon candidate

In a separate daemon checkout, build the real non-publishing snapshot artifacts:

```sh
make package
```

Then, from the role checkout and matching controller environment:

```sh
python tests/vm/run.py --platform rocky-amd64 \
  --workdir "${HOME}/perimeterd-role-rocky-candidate" \
  --candidate-dist ../perimeterd/dist
```

Repeat with `debian-amd64` and `fedora-arm64`, their matching environments, separate work directories and the appropriate packages from the same original dist. Candidate validation also requires the host's `dpkg-deb` for Debian or `rpm` for RPM platforms. The dist must contain original GoReleaser `metadata.json`, `checksums.txt` and selected native package bytes; the [candidate helper](../tests/vm/candidate.py) verifies metadata, checksum records and native package name/version/architecture before booting.

A restricted local CONNECT/TLS fixture serves synthetic candidate release metadata and unchanged local package/checksum bytes at the official GitHub URLs. Its temporary CA is trusted only in explicit test settings; production URLs, TLS validation and global trust stores are not changed. Candidate mode never selects a hosted prerelease or requires an older development-release migration fixture.

Each owned run uses two fresh overlays: the current-format lifecycle exercises ownership, packet enforcement, service/backend/reload/retry, check/no-op and stopped-state behavior; the fresh-start case introduces a byte-identical binary replacement to exercise stale-inode recovery. Do not use these scenarios against a production host. `--external-port` is only for an already isolated disposable guest in published mode, requires `--identity`, and cannot be combined with `--candidate-dist` or `--require-kvm`: the harness cannot certify an external VM's accelerator. Candidate mode supports `--require-kvm` and still owns both overlays.

### Timings and diagnostics

Each invocation initializes `workdir/logs/timings.jsonl`, then appends and flushes records as scopes finish, with concise `TIMING` completion lines in the live log. Elapsed seconds use a monotonic clock. Records contain `platform`, `mode` (`published`/`candidate`), `scenario` (null for run-wide work), `kind` (`run`/`scenario`/`phase`/`play`), `label`, numeric `elapsed_seconds`, `outcome`, `accelerator` (`kvm`/`tcg`, or `unknown`/`external`) and `version` (a concrete tag when known, otherwise null; never an unresolved `latest-prerelease`).

Run-wide phases measure image fetch/checksum verification and shared key/candidate-fixture setup. Each owned scenario measures `boot-readiness`, `prerequisites`, `fixture-stack`, `lifecycle` and `cleanup`; every labeled play includes its Ansible process and harness outcome assertions. Scenario totals include diagnostics and teardown, and the harness total includes shared teardown. External mode retains its single lifecycle with run/lifecycle/play records, but no fabricated owned-boot phases.

Durations are nested: plays are inside lifecycle phases, phases inside scenario totals, scenarios inside the run. Do not sum all records. Compare scenario totals and run-wide setup separately, then use phase/play costs to locate the bottleneck. The optimistic split bound is shared setup plus the slower scenario; independent jobs also duplicate setup/image transfer and add scheduling overhead. Collect comparable timings before proposing a separate matrix change; this harness does not split scenarios.

A verified expected Ansible rejection records `expected_failure`, not a failed scenario. Unexpected results and recap/no-op/release-resolution assertions record `failed`; timeouts retain partial play output and record `timeout`; interruptions record `interrupted` when Python can unwind. Entered scopes emit records without suppressing the original failure or cleanup. Completed records survive later failures, but abrupt runner termination cannot guarantee a final record. Argument/candidate validation may fail before timing scopes are entered.

CI attempts artifact upload on successful and failed jobs, best-effort on cancellation, using only `logs/`, `current-format/logs/` and `fresh-stale-inode/logs/`. These retain timings, consoles, per-play output, guest-stack information and failure diagnostics, not VM disks, SSH keys or fixture trust material. Missing logs from an early setup failure produce an upload warning. Local work directories retain the same evidence after cleanup.

## Hosted CI and dependency maintenance

[Role CI](../.github/workflows/ci.yml) runs on pull requests, pushes to `main`, and reusable `workflow_call`. Pull requests run controller fixtures, smoke syntax and modern lint without VM execution or publication credentials. The VM job is conditional on the event being `push`: it runs on main pushes and when [signed-tag publication](releasing.md) calls reusable CI from its tag-push event. Release gates run at that tag, rather than trusting a previous main-push result. Successful and failed VM jobs upload the diagnostic directories described above. These are configured procedures, not a claim that a hosted run has completed.

[Renovate policy](../.github/renovate.json) uses the recommended preset, a dependency dashboard and immutable Action commit pins. Major upgrades are reviewed independently. Non-major changes are grouped as CI setup actions, CI artifact actions, CI runner images, modern collections, legacy collections and Ansible development tools. A custom regex manager tracks the workflow's exact linter pin.

The Galaxy manager includes both collection requirement files and bumps minimums without replacing compatibility ranges with exact pins. Renovate updates for `community.general` are disabled in both files; its compatibility requirements are maintained manually, with the legacy upper bound remaining below 12. Controller core/Python lines, the role's minimum core version, VM image URL/checksum pairs and explicit daemon fixture selections are manually coordinated. Automatic updates to `actions/setup-python`'s Python-version inputs are disabled. Update related compatibility documentation and gates together; keep source pins in their workflows and harnesses.
