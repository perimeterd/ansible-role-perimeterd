# Changelog

Role release tags are independent of perimeterd daemon release tags. This changelog describes the Ansible role, not the upstream daemon.

## Unreleased

- Add opt-in CrowdSec bouncer key provisioning through `perimeterd_crowdsec_api_key`, with private root-owned directory/file permissions and suppressed secret output.
- Apply key-only rotations through acknowledged reloads and retry unacknowledged changes without starting deliberately stopped services.
- Reject relative managed CrowdSec credential paths before creating the credential directory or changing its ownership or permissions.
- Resolve Python Ruff findings in release tooling and VM/resolver tests without changing subprocess failure handling.

## 0.0.1

Initial release.
