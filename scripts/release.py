#!/usr/bin/env python3
"""Admit signed stable role tags and publish the CI-tested GitHub release."""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"
STABLE = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", re.ASCII)


def git(*args, env=None):
    return subprocess.check_output(["git", *args], text=True, env=env).strip()


def admit(ref, sha, public_keys):
    tag = ref.removeprefix("refs/tags/")
    if not ref.startswith("refs/tags/") or not STABLE.fullmatch(tag):
        raise ValueError("stable role tags must be bare MAJOR.MINOR.PATCH without leading zeroes")
    commit = git("rev-parse", "HEAD^{commit}")
    if commit != git("rev-parse", "--verify", f"{sha}^{{commit}}"):
        raise ValueError("checkout does not match the triggering commit")
    if git("rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}") != commit:
        raise ValueError("tag does not point to the triggering commit")
    subprocess.run(["git", "merge-base", "--is-ancestor", commit, "refs/remotes/origin/main"], check=True)
    if not public_keys.strip():
        raise ValueError("RELEASE_SIGNING_PUBLIC_KEYS must contain trusted OpenPGP release public keys")
    if git("cat-file", "-t", f"refs/tags/{tag}") != "tag":
        raise ValueError("role releases require a signed annotated tag")
    headers = git("cat-file", "-p", f"refs/tags/{tag}").partition("\n\n")[0].splitlines()
    if f"tag {tag}" not in headers:
        raise ValueError("signed tag annotation does not name the requested role version")
    # Only the configured public keys may authorize publication, never an ambient keyring.
    with tempfile.TemporaryDirectory(prefix="perimeterd-role-release-gpg-") as home:
        env = dict(os.environ, GNUPGHOME=home)
        with open(os.path.join(home, "gpg.conf"), "w", encoding="utf-8") as config:
            config.write("no-auto-key-retrieve\n")
        subprocess.run(["gpg", "--batch", "--import"], input=public_keys, text=True, env=env, check=True)
        verification = subprocess.run(
            ["git", "-c", "gpg.format=openpgp", "-c", "gpg.program=gpg", "verify-tag", "--raw", f"refs/tags/{tag}"],
            env=env, text=True, capture_output=True,
        )
        sys.stderr.write(verification.stderr)
        verification.check_returncode()
        if not any(line.startswith("[GNUPG:] VALIDSIG ") for line in verification.stderr.splitlines()):
            raise ValueError("role releases require a verified OpenPGP signature, not an ambient SSH signer")
    return {"tag": tag, "commit": commit}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        return None


def api_request(method, repository, path, token, payload=None):
    if not token:
        raise ValueError("GH_TOKEN is required for GitHub release publication")
    request = urllib.request.Request(
        f"{API}/repos/{repository}/{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json", "X-GitHub-Api-Version": "2022-11-28"},
    )
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        error.close()
        raise


def validate_release(document, tag):
    if (not isinstance(document, dict) or document.get("tag_name") != tag
            or document.get("draft") is not False or document.get("prerelease") is not False
            or type(document.get("id")) is not int or document["id"] <= 0
            or not isinstance(document.get("html_url"), str) or not document["html_url"]):
        raise ValueError("GitHub release does not match the tested stable tag and exact commit")
    return document["html_url"]


def publish(tag, tag_object, repository, token):
    encoded_tag = urllib.parse.quote(tag, safe="")
    remote = api_request("GET", repository, f"git/ref/tags/{encoded_tag}", token)
    if (not isinstance(remote, dict) or remote.get("ref") != f"refs/tags/{tag}"
            or not isinstance(remote.get("object"), dict)
            or remote["object"].get("type") != "tag" or remote["object"].get("sha") != tag_object):
        raise ValueError("remote tag no longer matches the verified signed annotation")
    try:
        document = api_request("GET", repository, f"releases/tags/{encoded_tag}", token)
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        document = api_request("POST", repository, "releases", token, {
            "tag_name": tag, "name": tag,
            "draft": False, "prerelease": False, "generate_release_notes": True,
        })
    # Reruns reuse only a matching published release; never edit or replace another identity.
    # target_commitish is advisory for existing tags; the verified Git object binds the commit.
    return validate_release(document, tag)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("admit", "publish"))
    args = parser.parse_args()
    metadata = admit(os.environ["GITHUB_REF"], os.environ["GITHUB_SHA"],
                     os.environ.get("RELEASE_SIGNING_PUBLIC_KEYS", ""))
    if args.action == "admit":
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            for key, value in metadata.items():
                output.write(f"{key}={value}\n")
        print(json.dumps(metadata))
    else:
        if metadata != {"tag": os.environ["ROLE_TAG"], "commit": os.environ["COMMIT"]}:
            raise ValueError("publication no longer matches the CI-tested role identity")
        print(publish(metadata["tag"], git("rev-parse", f"refs/tags/{metadata['tag']}"),
                      os.environ["GITHUB_REPOSITORY"], os.environ.get("GH_TOKEN", "")))


if __name__ == "__main__":
    main()
