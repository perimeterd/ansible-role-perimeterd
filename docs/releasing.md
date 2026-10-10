# Releasing the standalone role

Role versions are independent of daemon versions; a daemon release does not define a role tag. This guide describes the configured release procedure; it does not claim a hosted CI run, GitHub role release or Galaxy import has executed. Operator installation and runtime contracts belong in the [README](../README.md); local maintainer checks are in [development](development.md).

## Prerequisites

- Merge reviewed role changes into `main` and update the [changelog](../CHANGELOG.md). Choose an unused role version independently of the daemon version.
- Have the designated OpenPGP release signing private key available on the signing machine. Export its ASCII-armored **public** key into the repository Actions secret `RELEASE_SIGNING_PUBLIC_KEYS`, or an organization secret granting this repository access. Do not use an Actions variable or upload a private key. Include only keys authorized to release the role.
- The [`perimeterd` Galaxy namespace](https://galaxy.ansible.com/ui/standalone/namespaces/29189/) is established. Ensure the authorized Galaxy account has repository import access and that the intended repository maps uniquely to `perimeterd.perimeterd`; namespace creation is not a pending prerequisite.
- Configure a GitHub environment named `galaxy` with its `GALAXY_API_TOKEN` secret and applicable tag/deployment policies in repository **Settings → Environments → galaxy**.
- Allow Actions to use its workflow token for GitHub release publication. Only the publication job requests `contents: write`; admission, CI and import retain read-only repository permissions. The import job uses Python 3.14 and the latest core 2.21 patch for the Galaxy CLI.

## Sign and push the selected version

Accepted tags are signed annotated OpenPGP **bare stable SemVer**: `MAJOR.MINOR.PATCH`, with no `v` prefix, leading zeroes, prerelease suffix or build suffix. Lightweight tags, unsigned annotations, SSH signatures and aliases to an annotation naming a different version are rejected. The tagged commit must be reachable from `origin/main`; it may remain eligible after `main` advances. There is no main-push development-release channel for the role.

Before tagging:

- Wait for all controller and VM jobs on the final reviewed `main` commit, including the documentation/changelog changes, to pass. The tag workflow will run the gates again at the tagged commit.
- Confirm the changelog has an entry for the selected version and the tag is unused. No separate role-version field needs updating in `meta/main.yml`; the signed tag supplies the version.
- Confirm `RELEASE_SIGNING_PUBLIC_KEYS` contains the intended public signing key and `GALAXY_API_TOKEN` is available in the `galaxy` environment. Secret presence alone does not prove key/token validity.
- Ensure the environment's deployment policy allows the selected tag.
- Ensure the published daemon selected by the README's default `latest` has packages for your target and supports acknowledged reload. CI's published VM scenarios select `latest-prerelease`, so passing them does not establish compatibility of the stable daemon.

Run from that reviewed commit in the role checkout:

```sh
read -r -p 'Role version (MAJOR.MINOR.PATCH): ' role_version
fingerprint=$(git config user.signingkey)
# Export and configure this exact public key as RELEASE_SIGNING_PUBLIC_KEYS first.
gpg --armor --export "$fingerprint"
git -c gpg.format=openpgp tag -u "$fingerprint" "$role_version" -m "Release $role_version"
git -c gpg.format=openpgp verify-tag "$role_version"
git push origin "refs/tags/$role_version"
```

Local verification is useful but cannot substitute for workflow admission. Never move or replace a published tag, and do not manually create the GitHub release as an alternative publication path.

## Admission, exact-commit gates and publication

The [release workflow](../.github/workflows/release.yml) starts on pushes matching its numeric tag glob; the stricter [release script](../scripts/release.py) decides whether the tag is admissible.

1. **Admit the signed identity.** With full history and freshly fetched `origin/main`, admission verifies that the checkout, triggering SHA and tag all resolve to the same commit, that the commit is a main ancestor, and that the annotated tag's internal name matches the requested role version. Signature verification runs in a temporary keyring populated only from `RELEASE_SIGNING_PUBLIC_KEYS`, with automatic key retrieval disabled. It requires native OpenPGP verification and a GPG `VALIDSIG` result; ambient runner keys or SSH signer trust cannot authorize release.
2. **Run CI at the tag.** The workflow calls [Role CI](../.github/workflows/ci.yml) at the triggering tag, not a lookup of an earlier successful main run. Required gates include controller fixtures and smoke syntax on core 2.16/Python 3.12 and core 2.21/Python 3.14, strict lint on 2.21, and all three native VM profiles documented in [development](development.md). The reusable workflow retains the tag-push event, satisfying the VM job's `push` condition. Failed admission or CI prevents publication.
3. **Publish only the tested identity.** The publication job checks out the admitted exact commit, fetches main and reruns admission. Its tag/commit must still match admission outputs. Before querying or creating a release, the script asks the GitHub API for the remote tag ref and requires an annotated tag object with the same object SHA as the locally verified signed annotation. A moved or substituted remote tag fails closed.
4. **Create or reuse a stable GitHub release.** Only a release lookup returning HTTP 404 permits creation, using the already-existing tag, generated notes, `draft: false` and `prerelease: false`. A successful lookup or creation response must identify that tag, a published stable channel, a positive integer release ID and a nonempty release URL. The signed Git tag object binds the tested commit; release `target_commitish` is advisory for a preexisting tag and is not used as commit proof.
5. **Import into Galaxy.** After publication, the import job enters the `galaxy` environment, checks out the admitted commit, fetches main and reverifies the signed tag. Imports are serialized with the `galaxy-role-import` concurrency group and are not cancelled in progress. The job runs:

   ```sh
   ansible-galaxy role import perimeterd ansible-role-perimeterd \
     --role-name perimeterd --branch "$ROLE_TAG" --timeout 600
   ```

   The workflow supplies the token through `ANSIBLE_GALAXY_SERVER_PUBLISH_TOKEN`, sourced from `GALAXY_API_TOKEN`, with the configured Galaxy API URL and server list. It never puts the token in command-line arguments. An empty token, failed import command or output reporting multiple associated Galaxy roles fails the job. It waits for the import result; do not treat GitHub release creation alone as successful Galaxy publication.

Release creation and import intentionally remain in one workflow: a release created with `GITHUB_TOKEN` does not trigger a separate `release: published` workflow.

## Failure and rerun semantics

- Admission and exact-tag CI failures stop publication. Correct the underlying issue; do not bypass the gates with a manual release or weaken signing trust.
- GitHub lookup errors other than the missing-release 404 fail closed. Existing draft/prerelease releases, mismatched tags, malformed release responses and changed remote tag objects are rejected, not edited or replaced.
- Creation is not automatically retried after an uncertain API response. An authorized workflow rerun can recover a release that was actually created: it reverifies the identity and reuses only the matching published stable release without editing it.
- GitHub publication can succeed while Galaxy import fails. A rerun reuses the matching GitHub release and can attempt the import again; import is not skipped merely because the release exists. Resolve token, access, environment-policy or ambiguous-mapping failures through the authorized services. The workflow does not roll back or delete the GitHub release on import failure.
- Never resolve a failure by retagging a published version. If released source needs correction, review and release a new independent role version.

## Verify the published role

After import completes, confirm Galaxy lists `perimeterd.perimeterd` at the released version. In a modern controller environment, exercise a clean install pinned to that version and verify named-role resolution:

```sh
read -r -p 'Released role version (MAJOR.MINOR.PATCH): ' role_version
verify_dir=$(mktemp -d)
export ANSIBLE_ROLES_PATH="$verify_dir/roles"
export ANSIBLE_COLLECTIONS_PATH="$verify_dir/collections"
ansible-galaxy role install "perimeterd.perimeterd,$role_version" \
  --roles-path "$ANSIBLE_ROLES_PATH"
ansible-galaxy collection install -r requirements-collections.yml \
  --collections-path "$ANSIBLE_COLLECTIONS_PATH"
cat > "$verify_dir/verify.yml" <<'YAML'
---
- name: Verify the published role resolves
  hosts: localhost
  gather_facts: false
  roles:
    - role: perimeterd.perimeterd
YAML
ansible-playbook -i localhost, --syntax-check "$verify_dir/verify.yml"
```

Run this from the role checkout in a disposable shell so the temporary lookup paths do not leak into your normal controller environment. Syntax checking does not install or start the daemon. For runtime acceptance of the README defaults, use the installed role against a disposable systemd host with firewall prerequisites and recovery access; apply twice and confirm the second run has no changes, then change configuration and confirm acknowledged reload succeeds.

Check the hosted job and Galaxy import results rather than inferring publication from local signed-tag/API fixtures. GitHub release creation alone is not completion; the Galaxy version and clean install must also succeed.
