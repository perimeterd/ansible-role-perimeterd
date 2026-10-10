# perimeterd.perimeterd

Deploy and manage [perimeterd](https://github.com/perimeterd/perimeterd) with Ansible. Install official Linux packages, validate your firewall configuration, and keep the systemd service in the requested state.

- **Track stable releases or pin a version.** Updates happen when you run Ansible, not through a background updater.
- **Validate before replacing configuration.** Configuration-only changes use acknowledged reloads; package changes restart the service when needed.
- **Converge without unnecessary restarts.** Manage boot enablement and running state independently, including stopped installations.
- **Use native packages.** Install checksum-verified DEB/RPM assets with APT, DNF4, DNF5, or Zypper on amd64 and arm64 hosts.

The role's Galaxy name is `perimeterd.perimeterd`, in the [perimeterd namespace](https://galaxy.ansible.com/ui/standalone/namespaces/29189/). Role releases and daemon releases have independent version numbers.

## Requirements

- **Controller:** Ansible with `ansible-core` 2.16 or newer and a compatible controller Python. Use the latest 2.21 patch for modern targets. Install `community.general` as shown below.
- **Managed host:** Linux with systemd, a supported DEB/RPM package manager, compatible target Python, trusted TLS certificates, and privilege escalation to root.
- **Firewall readiness:** provision the tools and kernel capabilities needed by your chosen backend. The role does not select firewall tools from your configuration.
- **Network access:** the controller needs GitHub API access; managed hosts need access to official GitHub release downloads and their package repositories.

Support is capability-based, not an OS-version allowlist. See [compatibility and prerequisites](docs/reference.md) for details, including the isolated legacy core 2.16 environment for Python 3.6 targets. Core 2.16 is upstream end-of-life.

## Install

Create `requirements.yml` in your Ansible project:

```yaml
---
roles:
  - name: perimeterd.perimeterd
collections:
  - name: community.general
    version: '>=11.4.9'
```

Install both requirements into your controller environment:

```sh
ansible-galaxy role install -r requirements.yml
ansible-galaxy collection install -r requirements.yml
```

The role version is intentionally unpinned, so a fresh install selects the latest imported role release. For reproducible deployments, add a `version` field with a published role tag. This does not pin the daemon. For legacy core 2.16, use the collection range `'>=11.4.9,<12.0.0'` in a separate collection path or execution environment.

## Quick start

Save this as `perimeterd.yml`, with an `edge_firewalls` group in your inventory:

```yaml
---
- name: Manage perimeterd
  hosts: edge_firewalls
  become: true
  serial: 1
  roles:
    - role: perimeterd.perimeterd
      vars:
        perimeterd_version: latest
        perimeterd_config:
          version: 1
          firewall:
            backend: nftables
          policies: []
```

```sh
ansible-playbook -i inventory.ini perimeterd.yml
```

`latest` follows GitHub's designated latest stable perimeterd release on each Ansible run. Configuration-only changes require the acknowledged-reload interface described in the [operator reference](docs/reference.md#controller-and-host-requirements).

**This starts the firewall manager, even with an empty policy list. It is not a dry run.** The role owns the entire `/etc/perimeterd/perimeterd.yaml`; it does not merge your mapping with existing configuration. On existing installations, supply your complete intended configuration rather than replacing it with this baseline. Prepare console recovery and roll out serially before applying live firewall changes.

Credential files and external integrations remain yours to provision.

## Common choices

Set these variables in inventory or the role's `vars` mapping.

### Pin the daemon

For example, pin an exact published daemon tag:

```yaml
perimeterd_version: '0.0.1'
```

Pinning the role does **not** pin the daemon. Exact daemon pins are still resolved through GitHub on each run. Older native packages require an explicit downgrade opt-in; see the [operator reference](docs/reference.md).

### Opt into prereleases

```yaml
perimeterd_version: latest-prerelease
```

This selects the newest published prerelease within the first 100 releases returned by GitHub. There is no stable/prerelease fallback; use an exact published tag when you need a specific build.

### Install without starting

```yaml
perimeterd_service_state: stopped
perimeterd_service_enabled: false
```

Package and configuration changes keep the service stopped. Stopping is not uninstalling or cleaning up firewall state; the role preserves daemon-owned persistent state.

## Before rollout

- `--check` resolves the release and validates its checksum manifest. It does **not** validate your daemon configuration or prove that installation, startup, or reload will succeed.
- Package installation and configuration changes are **not an atomic transaction**. A failed validation after an upgrade can leave the new package installed with the old configuration and running process. Correct the configuration and rerun; there is no automatic rollback.
- Configuration output is hidden by default. Do not enable `perimeterd_config_show_diff` when the new configuration contains secrets.
- Download checksums provide integrity against the official manifest, not independent signature or provenance verification.

## Documentation

- [Operator reference](docs/reference.md): variables, version selection, compatibility, secrets, trust, and recovery.
- [Architecture](docs/architecture.md): task flow, service decisions, acknowledged reloads, and interrupted-action recovery.
- [Development](docs/development.md): local checks, VM scenarios, CI coverage, and dependency maintenance.
- [Releasing](docs/releasing.md): signed role tags, publication gates, and Galaxy import.
- [Changelog](CHANGELOG.md): role changes, separate from daemon releases.

## Support and license

Report role installation or automation issues in the [role issue tracker](https://github.com/perimeterd/ansible-role-perimeterd/issues). For daemon behavior and policy configuration, see the [perimeterd project](https://github.com/perimeterd/perimeterd).

[MIT license](LICENSE).
