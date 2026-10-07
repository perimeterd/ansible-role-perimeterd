# Operator reference

The role installs official perimeterd native packages, owns the complete daemon configuration, and reconciles the packaged systemd service. Start with the [usage guide](../README.md); [architecture](architecture.md) explains task ordering and service decisions. Role and daemon versions are independent.

## Public variables

These are the eight supported inputs. Internal `_perimeterd_*` facts are implementation details.

| Variable | Default | Contract |
| --- | --- | --- |
| `perimeterd_version` | `latest` | `latest`, `latest-prerelease`, or an exact published SemVer tag, optionally containing its actual `v` prefix. |
| `perimeterd_allow_downgrade` | `false` | Boolean permitting native package downgrade handling; not permission to discard or convert daemon state. |
| `perimeterd_config` | See below | Complete, nonempty daemon configuration mapping with a `firewall` mapping. It replaces the configuration; it does not merge old or default keys. An explicit backend must be `nftables` or `iptables`. |
| `perimeterd_service_enabled` | `true` | Boolean controlling systemd boot enablement independently of running state. |
| `perimeterd_service_state` | `started` | `started` or `stopped`. A deliberately stopped service stays stopped across package and configuration changes. |
| `perimeterd_config_show_diff` | `false` | Boolean enabling output of the new configuration after successful validation and a content change. The old configuration is redacted; the new configuration is **not** secret-redacted. |
| `perimeterd_github_token` | `''` | Optional controller-side GitHub API read token. It is not sent with target artifact requests or written to the target. |
| `perimeterd_download_timeout` | `60` | Numeric request timeout in seconds, converted to an integer that must be positive. Applies to release metadata, checksum manifest and package downloads. |

Default complete configuration:

```yaml
perimeterd_config:
  version: 1
  firewall:
    backend: nftables
  policies: []
```

This is an empty-policy baseline, not a production firewall policy.

## Release selectors

- **Exact tag:** requests that tag's GitHub release and requires exact returned tag equality. For example, `0.0.1` and `v0.0.1` are distinct tag spellings; the role never adds or removes `v` for lookup. Stable or prerelease tags are allowed, but drafts and unpublished releases are rejected. A missing tag never falls back.
- **`latest`:** uses GitHub's designated latest stable release endpoint, not an independent SemVer sort. A missing stable release fails rather than selecting a prerelease.
- **`latest-prerelease`:** makes one release-list request with `per_page=100`. Among non-draft prereleases in that response, it chooses the greatest `published_at`, breaking ties by numeric release ID. It does not follow pagination or compare SemVer. No candidate in that window is an error; use an exact tag for a release outside it. This is bounded discovery, not a guarantee about all release history.

Each host resolves again on every invocation, including check mode. There is no background updater or cross-channel fallback. The selected release must have exactly one package matching its tag, the host package family and architecture, and exactly one `checksums.txt`. Missing or ambiguous assets fail. Floating selectors are subject to the same native downgrade guard as exact pins.

## Controller and host requirements

**Controller:** the role minimum is ansible-core 2.16; use the latest 2.21 patch by default. Select controller and target Python versions supported by that core line. The 2.16 path supports legacy targets such as RHEL 8's Python 3.6, but core 2.16 is upstream end-of-life and does not regain security maintenance through this role. Core 2.21 cannot manage Python 3.6 targets.

Install `community.general` from [requirements-collections.yml](../requirements-collections.yml) (`>=11.4.9`) even when the target does not use Zypper: Ansible must resolve its module while loading the package tasks. Only isolated core 2.16 environments use [requirements-collections-legacy.yml](../requirements-collections-legacy.yml) (`>=11.4.9,<12.0.0`). A Python virtual environment alone does not isolate Ansible collection paths. The controller needs trusted CA roots and outbound HTTPS to `api.github.com`; it does not need `gh`, Go or Syft to run the role. An optional read token raises API limits; authentication, permission and rate-limit failures are explicit errors.

**Managed host:** Linux, x86_64/amd64 or aarch64/arm64, APT, DNF4, DNF5 or Zypper, a working systemd manager, and target Python supported by the selected ansible-core. Supply root escalation in the play. Provide trusted CA roots and outbound HTTPS to `github.com`, official release-asset redirect hosts and required native repositories. The role does not bootstrap CA roots. Support is capability-based, not an OS-name/version allowlist; Galaxy platform metadata is not a promise that every historical release works. [Development](development.md) owns the CI matrix.

Configuration-only reload requires the acknowledged `perimeterd reload --expect-config-sha256` command and the running daemon's private reload endpoint. A CLI or running daemon without that capability fails rather than falling back to signal delivery.

### Dependencies and firewall backend

Native package metadata and the host package manager resolve the selected release's dependencies and alternatives. An existing provider can satisfy a declared alternative. The role does not maintain its own daemon dependency list. It also ensures `xz-utils` on APT hosts solely for Ansible's local-DEB handling.

The role does not choose or install firewall tools from `perimeterd_config`; changing the configured backend does not install its tools. The operator supplies the selected backend's tools, coherent iptables alternatives, required IPv4/IPv6 kernel capabilities, module-loading policy and pre-existing attachment chains. The role does not inspect kernel modules, run `modprobe`, or configure persistent module loading. Offline validation cannot establish runtime backend readiness.

## Ownership and secrets

The role manages `/etc/perimeterd/perimeterd.yaml` as root-owned `0600` in a root-owned `0700` directory. It validates a candidate with the installed `/usr/bin/perimeterd` before atomic replacement, and also validates unchanged configuration with that executable. Keep the complete mapping in Vault-protected inventory when it contains secrets. Candidate values, validator output and normal Ansible diffs are suppressed. `perimeterd_config_show_diff: true` prints the new mapping without secret redaction; do not enable it for sensitive configuration.

Credential files, CrowdSec/OpenZiti enrollment and other secret-file provisioning belong to the caller. Deploy them before referencing their paths. Offline validation does not read credentials, authenticate, resolve feeds or test external systems. The optional GitHub token belongs on the controller, preferably in Vault or an environment lookup; API requests and token-bearing facts use `no_log`.

The native package owns the executable and systemd unit; the role supplies no replacement unit. The role retains verified artifacts in private `/var/cache/perimeterd` and its own application marker under `/var/lib/perimeterd-ansible`. Daemon runtime state under `/var/lib/perimeterd` remains daemon-owned. The role does not uninstall perimeterd, wipe firewall rules or runtime state, or clean arbitrary caches. Stopping the daemon does not remove its committed static firewall policy.

## Integrity and package transactions

Official GitHub metadata and downloads use HTTPS with TLS certificate validation. Asset metadata must point to the official repository release URLs. The checksum manifest must consist of valid SHA-256 basename records and contain exactly one record for the selected package; `get_url` verifies that digest before installation. The cache filename includes the digest and asset name.

TLS plus a checksum published beside the package establishes transfer integrity against that manifest, **not independent publisher authenticity or cryptographic provenance**. The role does not verify attestations or an independently signed manifest.

Repository signature checking follows operator-configured native repository policy. The role does not change global settings or claim checks are enabled if the operator disables them. For the verified local RPM only, DNF uses `--setopt=localpkg_gpgcheck=0` rather than `--nogpgcheck`; Zypper uses `--allow-unsigned-rpm` with `disable_gpg_check: false`. Repository signature policy remains intact.

Native managers determine equality, version ordering and installability; the role does not independently compare versions or assert the final installed version. With downgrades disabled, APT uses its native downgrade protection and RPM hosts first run an RPM-native test guard. Enabling the option permits native downgrade handling, not a separate forced-version guarantee. Package conffile policy preserves existing configuration until the role validates its replacement.

## Check mode

Check mode gathers facts, validates inputs, resolves release metadata on the controller and fetches/parses the checksum manifest on the target. It still needs network access, target Python and privileges required by the play. It does **not** download or install the package, inspect native downgrade compatibility, validate/render daemon configuration, change enablement, or start/stop/restart/reload the service. It predicts no installation or lifecycle changes. A successful check run is not evidence that a normal deployment succeeds.

## Failure, recovery and downgrade compatibility

Installation is not one atomic transaction. A rejected downgrade can leave a downloaded artifact; APT can also install `xz-utils` and update its package cache before rejecting the daemon package. A later configuration or service failure can leave the selected package installed. Invalid configuration is not atomically installed, and configuration failure prevents service reconciliation; existing configuration and a running old executable can remain.

After correcting the cause, rerun the role. A stale running executable is detected even if the new run's package transaction reports no change. Failed service handling leaves the application marker unadvanced, so unapplied configuration is retried. See [service reconciliation](architecture.md#service-reconciliation) for exact precedence and marker limits.

A reload rejected before application retains the previous policy. Degraded or unknown completion is not proof of rollback: preserve durable evidence and inspect the reported error and service journal. Maintain console recovery access when changing live firewall policy.

`perimeterd_allow_downgrade` authorizes package handling only; it does not establish compatibility with retained daemon state or configuration. The role does not migrate daemon-owned state. Use a daemon compatible with retained state, or recreate a disposable development host. Do not erase or relabel durable records to force an older daemon to start. [Releasing](releasing.md) owns role publication, not daemon-state migration.
