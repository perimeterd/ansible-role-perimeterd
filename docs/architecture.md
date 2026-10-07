# Role architecture

This document describes the current role implementation, not the daemon's internal engine. The [operator reference](reference.md) owns inputs, requirements, integrity guarantees and recovery cautions. [Development](development.md) owns tests and the CI matrix; [releasing](releasing.md) owns role publication.

## Task flow

[`tasks/main.yml`](../tasks/main.yml) runs these phases in order:

1. **Discover and validate:** gather minimal target facts, validate public controls and the complete configuration's basic shape, require supported Linux architecture, native package manager and systemd, then derive DEB/RPM and amd64/arm64 asset selection.
2. **Resolve:** [`resolve.yml`](../tasks/resolve.yml) captures this host's selector, timeout and token before delegated requests, resolves one official GitHub release, validates its identity and asset metadata, and selects one target package plus `checksums.txt`. Exact and floating selection rules are in the [reference](reference.md#release-selectors).
3. **Prepare and install:** [`install.yml`](../tasks/install.yml) fetches and validates the checksum manifest on the target, derives a digest-qualified private cache path, and downloads the verified package in normal mode. [`install-package.yml`](../tasks/install-package.yml) delegates version order, dependencies and installation to native managers. APT preserves existing conffiles; DNF compares pre/post RPM identities solely to report package change; Zypper uses its native module. RPM's test guard runs before installation when downgrades are disabled.
4. **Validate and render:** [`config.yml`](../tasks/config.yml) hashes the actual template output, prepares the private configuration directory, then validates a candidate with the installed executable before atomic replacement. If content is unchanged, it runs offline validation explicitly with the same executable. Optional new-configuration output follows successful validation.
5. **Reconcile service:** [`service.yml`](../tasks/service.yml) sets boot enablement, reads systemd status, chooses one lifecycle action, and records successful handling for a desired-running service.

The last two phases and package mutation are skipped in check mode; the [check-mode boundary](reference.md#check-mode) is deliberately read-only, not a simulation of deployment. The role uses ordered tasks rather than deferred handlers, so a configuration validation failure prevents subsequent service reconciliation. Package installation precedes validation: failure is not a rollback of that package transaction.

## Controller and target boundary

| Controller | Managed host |
| --- | --- |
| GitHub API release discovery, delegated to `localhost` with `become: false` | Platform facts, checksum manifest retrieval, package download and native installation |
| Per-host selector/token capture via `hostvars[inventory_hostname]`; token-bearing requests and facts are suppressed | Root-owned package cache, configuration directory/file and application marker |
| Jinja rendering and SHA-256 of the actual template output | Offline validation by installed `/usr/bin/perimeterd`, atomic configuration replacement, systemd lifecycle actions |
| Optional GitHub API read token | Reload CLI talks to the running daemon's private Unix socket; daemon owns source resolution, credentials, policy application and durable recovery |

Metadata resolution is per host, not `run_once`; hosts can use different selectors or tokens without borrowing delegated controls from another host. The token is used only for the controller API request. Checksum and package requests originate on the managed host without that token. Native repository access and daemon runtime source access are also host responsibilities, separate from controller release discovery.

The role does not supply a replacement executable or unit, interpret firewall dependency alternatives itself, provision enrollment credentials, or manipulate daemon-owned durable records. These boundaries and prerequisite details live in the [reference](reference.md).

## Service reconciliation

Boot enablement is independent of runtime state. After configuration validation succeeds, action precedence is:

| Condition, in priority order | Action |
| --- | --- |
| Desired state is `stopped` | Stop; do not start/reload/restart or advance the application marker. |
| Desired state is `started`, but systemd is not active | Start. |
| Active service, and package transaction changed or running executable is stale | Restart. |
| Active service, executable current, but application marker differs or is missing | Acknowledged digest-bound reload. |
| Active service and matching marker | Ensure started without restart or reload. |

For an active service, the role compares device and inode of `/usr/bin/perimeterd` with `/proc/<MainPID>/exe`. This detects an executable replaced by an earlier interrupted upgrade even when the current package task correctly reports no change. A missing running executable stat also counts as stale. Restart takes precedence over reload so the selected installed executable handles the configuration. Enablement-only changes do not restart a converged service.

### Acknowledged reload

The role runs:

```text
/usr/bin/perimeterd reload --expect-config-sha256 <rendered-config-digest>
```

The digest covers the UTF-8 bytes produced by [`perimeterd.yaml.j2`](../templates/perimeterd.yaml.j2), including the managed-file header and final newline; it is not a hash of a separately reconstructed mapping. The CLI sends the expected digest to the running daemon's private `/v1/reload` endpoint through `/run/perimeterd/lookup.sock`.

The daemon reads the configuration once, hashes those same bytes and rejects a mismatch before loading credentials, resolving sources or applying native policy. A successful command acknowledges that request's committed revision, completed required native work and runtime publication, healthy enforcement, and no pending recovery. It is not merely signal delivery, process liveness or a later health sample. A later reload or refresh can supersede the acknowledged revision.

An unsupported CLI/endpoint or rejected, degraded or unknown completion fails the task. There is no signal-only or `systemd state: reloaded` fallback. Start/restart follows the packaged systemd activation contract; it does not use the reload request protocol.

### Successful-application marker

After successful service handling for a desired-running service, the role writes root-owned `0600` `/var/lib/perimeterd-ansible/config-applied` in a root-owned `0700` directory:

```text
config_sha256=<rendered-config-digest>
package=<selected-package-asset-name>
```

For reload, this checkpoint follows the daemon's acknowledgement. Failed service actions stop the block before the marker write. A marker-write failure also leaves the invocation failed, and a later run can retry handling. A missing or mismatched marker requests reload even when the template reports unchanged content, recovering a run interrupted after atomic configuration installation. Interrupted package replacement is covered separately by the running-executable comparison.

The marker is a role checkpoint, not daemon durable state, a native package version assertion, or continuous health monitoring. It records neither external credential/feed contents nor later out-of-band runtime changes; an unchanged configuration digest does not detect changes to referenced files. The role never erases daemon-owned state to repair its checkpoint. Consult [failure and recovery](reference.md#failure-recovery-and-downgrade-compatibility) before acting on a failed deployment.
