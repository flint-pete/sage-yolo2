#!/usr/bin/env python3
"""
register-ecr-version.py — register a new app version in the Sage ECR *catalog*
via the ECR API, without using the portal web UI.

WHY THIS EXISTS
---------------
The SES scheduler validates a job's image against the ECR app *catalog*
(ecr.sagecontinuum.org), NOT against the raw Docker registry or the image
you sideloaded into k3s. If the catalog has no record for your exact
version, `sesctl submit` fails with:

    [registry.sagecontinuum.org/<ns>/<name>:<ver> does not exist in ECR]

The portal "Create App / add version" UI registers that catalog record for
you (and tries to build the image). But for Thor/arm64 NVIDIA plugins the
portal *build* crashes under QEMU — and we serve the actual image via the
local sideload anyway (SES pods use imagePullPolicy=IfNotPresent). So all
we actually need from ECR is the catalog *metadata* record. This script
creates it directly via the API.

It works by cloning an existing version's record (same app, any prior
version), bumping the version string and git source, and POSTing it to
/api/submit. Auth uses your Sage portal access token with the
'Authorization: Sage <token>' header scheme.

USAGE
-----
    python3 register-ecr-version.py \
        --namespace beckman \
        --name birdnet-species \
        --from-version 0.1.0 \
        --version 0.1.1 \
        --git-url https://github.com/flint-pete/birdnet.git \
        --token "$SAGE_TOKEN"          # or set SAGE_TOKEN env var

After it prints "registered: <ns>/<name>:<ver>", re-run `sesctl submit`.

NOTE: This registers catalog metadata only. The actual image must already
be pullable — either pushed to the registry (if you have push access) or
sideloaded into the node's k3s containerd (the usual Thor path).
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

ECR_API = "https://ecr.sagecontinuum.org/api"


def api(method, path, token, body=None):
    url = f"{ECR_API}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Authorization": f"Sage {token}",
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--namespace", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--from-version", required=True,
                    help="An existing registered version to clone metadata from")
    ap.add_argument("--version", required=True, help="The new version to register")
    ap.add_argument("--git-url", required=True,
                    help="GitHub repo URL, e.g. https://github.com/flint-pete/birdnet.git")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--arch", default="linux/arm64",
                    help="Comma-separated architectures (default: linux/arm64)")
    ap.add_argument("--token", default=os.environ.get("SAGE_TOKEN", ""),
                    help="Sage portal access token (or set SAGE_TOKEN env var)")
    args = ap.parse_args()

    if not args.token:
        sys.exit("ERROR: provide --token or set SAGE_TOKEN")

    ns, name = args.namespace, args.name

    # 1. Clone the known-good prior version record.
    status, resp = api("GET", f"/apps/{ns}/{name}/{args.from_version}", args.token)
    if status != 200:
        sys.exit(f"ERROR: could not read {ns}/{name}:{args.from_version} "
                 f"(HTTP {status}): {resp[:200]}")
    rec = json.loads(resp)

    # 2. Build the new payload: copy metadata, bump version + source.
    payload = {
        "namespace": ns,
        "name": name,
        "version": args.version,
        # 'description' is REQUIRED by /submit; fall back to a sane default.
        "description": rec.get("description") or f"{name} plugin",
        "authors": rec.get("authors", ""),
        "keywords": rec.get("keywords", ""),
        "license": rec.get("license", ""),
        "homepage": rec.get("homepage", ""),
        "funding": rec.get("funding", ""),
        "collaborators": rec.get("collaborators", ""),
        "baseCommand": rec.get("baseCommand", ""),
        "arguments": rec.get("arguments", ""),
        "inputs": rec.get("inputs", []),
        "metadata": rec.get("metadata", {}),
        "source": {
            "architectures": [a.strip() for a in args.arch.split(",")],
            "branch": args.branch,
            "directory": ".",
            "dockerfile": "Dockerfile",
            "url": args.git_url,
            "build_args": {},
        },
    }

    # 3. Register.
    status, resp = api("POST", "/submit", args.token, payload)
    already = (status == 500 and "already exists" in resp)
    if status != 200 and not already:
        sys.exit(f"ERROR: /submit returned HTTP {status}: {resp[:300]}")

    if already:
        print(f"already registered: {ns}/{name}:{args.version} (no change)")
    else:
        new_id = json.loads(resp).get("id", f"{ns}/{name}:{args.version}")
        print(f"registered: {new_id}")

    # 4. Confirm it is visible in the public catalog.
    status, resp = api("GET", f"/apps/{ns}/{name}", args.token)
    if status == 200:
        vers = [it.get("id") for it in json.loads(resp).get("data", [])]
        print("catalog now lists:")
        for v in vers:
            print(f"  {v}")
    print("\nNext: re-run `sesctl ... submit -j <job-id>` — validation should pass.")


if __name__ == "__main__":
    main()
