"""Submit a Python satellite node to a Synapse registry — the community author's one command.

This replaces assembling the multipart `POST /node-packages` by hand. It packages the node
(reusing `package.py`), signs in, and uploads it. The node lands as **PENDING_REVIEW**: it is
NOT runnable until an ADMIN approves it (that gate is the whole trust model — see
`nodes/README.md`). Submitting is not publishing.

Usage:
    python submit.py <node_dir> --api http://localhost:3000 --email you@example.com --password ...
    # add --register to create the account first (a community author's first time)

What it sends (multipart, kind=satellite):
    nodeType, name, version, contract (JSON) — from the node's own manifest
    language=python, lockfile=<requirements.txt content>
    code=<the tar.gz bundle>

Stdlib only (urllib), like the rest of the kit — no pip install to contribute a node.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request
import uuid

from package import build_bundle, emit_manifest  # same directory


def _multipart(fields: dict, files: dict) -> tuple[bytes, str]:
    """Encode text `fields` and binary `files` ({name: (filename, bytes)}) as multipart."""
    boundary = f"----synapse{uuid.uuid4().hex}"
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        parts.append(f"{value}\r\n".encode())
    for name, (filename, data) in files.items():
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode()
        )
        parts.append(b"Content-Type: application/octet-stream\r\n\r\n")
        parts.append(data)
        parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), boundary


def _post_json(api: str, path: str, body: dict, cookie: str = "") -> tuple[dict, str]:
    """POST JSON, returning (parsed_body, session_cookie). Raises with the server message."""
    data = json.dumps(body).encode()
    req = urllib.request.Request(f"{api}{path}", data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req) as res:
            set_cookie = res.headers.get("Set-Cookie", "")
            session = set_cookie.split(";")[0] if set_cookie else cookie
            return json.loads(res.read() or b"{}"), session
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{path} failed: {e.code} {e.read().decode(errors='replace')}")


def _login(api: str, email: str, password: str, register: bool) -> str:
    path = "/auth/register" if register else "/auth/login"
    _, cookie = _post_json(api, path, {"email": email, "password": password})
    if not cookie:
        raise SystemExit("sign-in returned no session cookie")
    return cookie


def main() -> None:
    ap = argparse.ArgumentParser(description="Submit a Python satellite node for review.")
    ap.add_argument("node_dir")
    ap.add_argument("--api", default="http://localhost:3000")
    ap.add_argument("--email", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--register", action="store_true", help="create the account first")
    args = ap.parse_args()

    node_dir = os.path.abspath(args.node_dir)
    lock_path = os.path.join(node_dir, "requirements.txt")
    if not os.path.isfile(lock_path):
        raise SystemExit(f"{node_dir} has no requirements.txt (the lockfile)")

    manifest = emit_manifest(node_dir)
    bundle = build_bundle(node_dir)
    with open(lock_path, encoding="utf-8") as f:
        lockfile = f.read()

    cookie = _login(args.api, args.email, args.password, args.register)

    fields = {
        "nodeType": manifest["nodeType"],
        "name": manifest.get("name", manifest["nodeType"]),
        "version": manifest["version"],
        "contract": json.dumps(manifest),
        "kind": "satellite",
        "language": "python",
        "lockfile": lockfile,
    }
    files = {"code": (f"{manifest['nodeType']}-{manifest['version']}.tar.gz", bundle)}
    body, boundary = _multipart(fields, files)

    req = urllib.request.Request(f"{args.api}/node-packages", data=body, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req) as res:
            result = json.loads(res.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise SystemExit(f"publish failed: {e.code} {e.read().decode(errors='replace')}")

    print(f"submitted {manifest['nodeType']} v{manifest['version']}")
    print(json.dumps(result, indent=2))
    print("\nStatus is PENDING_REVIEW — an ADMIN must approve it before it can run.")


if __name__ == "__main__":
    main()
