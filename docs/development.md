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
ansible-playbook -i localhost, --syntax-check tests/vm/resolve-release.yml
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
ansible-playbook -i localhost, --syntax-check tests/vm/resolve-release.yml
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

Hosted Debian and Rocky jobs require KVM and grant device access only to the current disposable runner user; a missing KVM character device or broken virtualization fails the job. Fedora ARM64 remains on hosted x86-64 runners with TCG, using independent `current-format` and `fresh-stale-inode` jobs. Cross-architecture emulation can take hours; the controller profiles, lifecycle assertions and timeout tests are unchanged.

Every harness-launched Ansible play uses SSH pipelining via its subprocess-local `ANSIBLE_PIPELINING=True` environment, including check/diff, service-only, expected-failure and no-op calls. No repository/global Ansible configuration or production role default is changed. Cloud-init retains the passwordless-sudo test user; direct SSH/SCP, guest interpreters and file-transfer tasks retain their existing behavior.

Use a separate `--workdir` per platform/run outside the role checkout. It retains image caches and diagnostics, including top-level `logs/` and scenario-specific `current-format/logs/` and `fresh-stale-inode/logs/`. Temporary SSH keys and overlays are removed and owned QEMU processes are terminated on exit. Without `--workdir`, the runner creates a temporary working directory; `--image-cache` can designate a shared image cache. Image URLs and checksums remain authoritative in the harness, not copied into this guide.

Both owned-overlay modes exercise real native packages, systemd and kernel firewall behavior in isolated user-mode networking. With no `--scenario` (or explicit `all`), local runs execute `current-format` then `fresh-stale-inode` in separate pristine overlays. Select either name to execute just that lifecycle; fresh-only execution does not need a previous install or overlay.

Published mode without `--release-tag` starts with `latest-prerelease`, records the first concrete resolved daemon tag, and reuses it across subsequent normal role calls and both selected overlays. `--release-tag TAG` instead pins the baseline before any play or image fetch. It requires an exact SemVer daemon tag, preserving optional `v`, prerelease and build spelling; moving selectors, malformed tags and whitespace are rejected before guest contact or workdir creation. Explicit negative-test version overrides remain intact, and each role invocation still validates exact-tag metadata and required package/checksum assets. This option does not select the role repository's release tag or guarantee immutable upstream assets.

A standalone smoke play can select the latest stable daemon with `-e smoke_version=latest`, or an exact published tag; run it only against a disposable guest. To reproduce Fedora's shared selection locally, activate Python 3.14/core 2.21 with the modern role/collection paths configured above:

```sh
release_dir=$(mktemp -d)
ansible-playbook -i localhost, tests/vm/resolve-release.yml \
  -e "vm_release_output=$release_dir/release.json" &&
TAG=$(python -c 'import json, sys; print(json.load(open(sys.argv[1]))["tag"])' \
  "$release_dir/release.json") &&
python tests/vm/run.py --platform fedora-arm64 \
  --scenario current-format --release-tag "$TAG" \
  --workdir "${HOME}/perimeterd-fedora-current" &&
python tests/vm/run.py --platform fedora-arm64 \
  --scenario fresh-stale-inode --release-tag "$TAG" \
  --workdir "${HOME}/perimeterd-fedora-fresh"
```

These are separate sequential local processes, not a parallel benchmark. The controller-only resolver uses the production role's `resolve` tasks for Fedora RPM/ARM64 assets; it neither installs a package nor downloads package/checksum bytes. CI reads its public JSON through a validated output bridge and passes the same exact tag to both shards. Failed/missing selection blocks Fedora without blocking Debian/Rocky. Rerun a failed shard with its prerequisite's retained output; if deliberately rerunning selection, rerun both dependent shards together for comparable evidence.

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

Each selected owned scenario uses a fresh overlay: `current-format` exercises ownership, packet enforcement, service/backend/reload/retry, check/no-op and stopped-state behavior; `fresh-stale-inode` introduces a byte-identical binary replacement to exercise stale-inode recovery. Candidate mode supports either selected scenario or the default pair, using the same validated original package/manifest and scoped TLS fixture; `--release-tag` is forbidden because candidate identity remains authoritative. Candidate mode also supports `--require-kvm`.

Do not use these scenarios against a production host. `--external-port` is only for an already isolated disposable guest in published mode and requires `--identity`. With no `--scenario`, it retains one `current-format` lifecycle and supports an exact `--release-tag`. Any explicit `--scenario` (including `all`), `--candidate-dist` or `--require-kvm` is rejected before contact or workdir creation: the harness cannot create fresh external overlays or certify an external VM's accelerator.

### Timings and diagnostics

Each invocation initializes `workdir/logs/timings.jsonl`, then appends and flushes records as scopes finish, with concise `TIMING` completion lines in the live log. Elapsed seconds use a monotonic clock. Records contain `platform`, `mode` (`published`/`candidate`), `scenario` (null for run-wide work), `kind` (`run`/`scenario`/`phase`/`play`), `label`, numeric `elapsed_seconds`, `outcome`, `accelerator` (`kvm`/`tcg`, or `unknown`/`external`) and `version` (a concrete tag when known, otherwise null; never an unresolved `latest-prerelease`).

Run-wide phases measure image fetch/checksum verification and shared key/candidate-fixture setup. Each owned scenario measures `boot-readiness`, `prerequisites`, `fixture-stack`, `lifecycle` and `cleanup`; every labeled play includes its Ansible process and harness outcome assertions. Scenario totals include diagnostics and teardown, and the harness total includes shared teardown. External mode retains its single lifecycle with run/lifecycle/play records, but no fabricated owned-boot phases.

Durations are nested: plays are inside lifecycle phases, phases inside scenario totals, scenarios inside the run. Do not sum all records. Compare scenario totals and run-wide setup separately, then use phase/play costs to locate the bottleneck. Fedora's jobs each repeat controller/image/shared setup on independent hosted runners; queueing and duplicated setup reduce the split's potential benefit. Measure prerequisite duration, per-shard queue/runtime and end-to-end hosted critical path before claiming a speedup. Local selected-scenario timings are not a hosted before/after benchmark.

A verified expected Ansible rejection records `expected_failure`, not a failed scenario. Unexpected results and recap/no-op/release-resolution assertions record `failed`; timeouts retain partial play output and record `timeout`; interruptions record `interrupted` when Python can unwind. Entered scopes emit records without suppressing the original failure or cleanup. Completed records survive later failures, but abrupt runner termination cannot guarantee a final record. Argument/candidate validation may fail before timing scopes are entered.

CI attempts artifact upload on successful and failed jobs, best-effort on cancellation. Debian/Rocky artifacts retain root `logs/` and both scenario log directories. Fedora artifacts are uniquely named `role-vm-fedora-arm64-current-format` and `role-vm-fedora-arm64-fresh-stale-inode`, each retaining only root `logs/` and its selected scenario's `logs/`. These contain timings, consoles, per-play output, guest-stack information and failure diagnostics, not VM disks, SSH keys or fixture trust material. Missing logs from an early setup failure produce an upload warning. Local work directories retain the same evidence after cleanup. Exact-tag startup logs and timing `version` fields identify the shared baseline even for deliberate negative plays.

## Hosted CI and dependency maintenance

[Role CI](../.github/workflows/ci.yml) runs on pull requests, pushes to `main`, and reusable `workflow_call`. Pull requests run controller fixtures, smoke/resolver syntax and modern lint without resolver/VM execution or publication credentials. All VM work and the Fedora resolver remain conditional on `push`: they run on main pushes and when [signed-tag publication](releasing.md) calls reusable CI from its tag-push event. The existing `vm` matrix runs Debian/Rocky independently with both scenarios; `fedora-release` selects one concrete daemon tag before the two-row `vm-fedora` matrix fans out with `fail-fast: false`. All use `ubuntu-26.04`; no custom runner, serial shard lock or permissive failure gate is introduced.

Release `checks` calls this reusable workflow and `publish` requires successful admission and checks. Resolver failure or either Fedora shard failure therefore prevents publication; neither shard can fall back to a moving selector. Release gates run at the admitted tag, not a previous main-push result. Fedora check names include the scenario: `native systemd VM (fedora-arm64, current-format, core 2.21)` and `native systemd VM (fedora-arm64, fresh-stale-inode, core 2.21)`, preceded by `Fedora ARM64 release selection`. Keep any required-status-check rules aligned with workflow job names. VM jobs are push-only, so do not require them on pull requests where they do not run.

[Renovate policy](../.github/renovate.json) uses the recommended preset, a dependency dashboard and immutable Action commit pins. Major upgrades are reviewed independently. Non-major changes are grouped as CI setup actions, CI artifact actions, CI runner images, modern collections, legacy collections and Ansible development tools. A custom regex manager tracks the workflow's exact linter pin.

The Galaxy manager includes both collection requirement files and bumps minimums without replacing compatibility ranges with exact pins. Renovate updates for `community.general` are disabled in both files; its compatibility requirements are maintained manually, with the legacy upper bound remaining below 12. Controller core/Python lines, the role's minimum core version, VM image URL/checksum pairs and explicit daemon fixture selections are manually coordinated. Automatic updates to `actions/setup-python`'s Python-version inputs are disabled. Update related compatibility documentation and gates together; keep source pins in their workflows and harnesses.
