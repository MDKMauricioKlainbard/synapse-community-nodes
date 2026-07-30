"""Package a Python satellite node directory into the artifacts you publish.

Given a node source dir (containing `main.py` and `requirements.txt`), this:
  * vendors this kit's `workflow_node.py` into the bundle (so `import workflow_node` works
    inside the dependency-free isolation floor),
  * builds the `code` tar.gz the publish endpoint accepts (validated by `assertSatelliteCode`:
    a `main.py` at the root declaring a top-level `def run(...)`),
  * writes `node.json` — the manifest / publish `contract` — by importing the node and
    reading `run.manifest`.

Usage:
    python package.py <node_dir> [out_dir]

Then publish (multipart) with: kind=satellite, language=python, the `lockfile` =
requirements.txt content, `code` = the tar.gz, and `contract` = node.json.
"""
import io
import json
import os
import sys
import tarfile

KIT_DIR = os.path.dirname(os.path.abspath(__file__))
SDK = os.path.join(KIT_DIR, "workflow_node.py")


def build_bundle(node_dir: str) -> bytes:
    """A tar.gz of the node's own files + the vendored SDK. Mirrors what an author uploads;
    what `isolation.py` later extracts and runs."""
    main_py = os.path.join(node_dir, "main.py")
    if not os.path.isfile(main_py):
        raise SystemExit(f"{node_dir} has no main.py")

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        # The node's own source (skip caches and any stale vendored copy — we add ours).
        for name in sorted(os.listdir(node_dir)):
            if name in ("__pycache__", "node.json", "workflow_node.py") or name.endswith(".pyc"):
                continue
            tar.add(os.path.join(node_dir, name), arcname=name)
        # Vendor the SDK at the bundle root.
        tar.add(SDK, arcname="workflow_node.py")
    return buf.getvalue()


def emit_manifest(node_dir: str) -> dict:
    """Import the node and read its declared manifest (the publish `contract`). Runs with
    the kit + node dir on sys.path so `import workflow_node` and any aux modules resolve."""
    sys.path.insert(0, KIT_DIR)
    sys.path.insert(0, node_dir)
    import importlib.util

    spec = importlib.util.spec_from_file_location("node_main", os.path.join(node_dir, "main.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    run = getattr(mod, "run", None)
    manifest = getattr(run, "manifest", None)
    if manifest is None:
        raise SystemExit("main.py has no `run = node(...)` at top level (no manifest)")
    return manifest.to_json()


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    node_dir = os.path.abspath(sys.argv[1])
    out_dir = os.path.abspath(sys.argv[2]) if len(sys.argv) > 2 else node_dir

    manifest = emit_manifest(node_dir)
    bundle = build_bundle(node_dir)

    os.makedirs(out_dir, exist_ok=True)
    node_json = os.path.join(out_dir, "node.json")
    code_tgz = os.path.join(out_dir, f"{manifest['nodeType']}-{manifest['version']}.tar.gz")
    with open(node_json, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    with open(code_tgz, "wb") as f:
        f.write(bundle)

    print(f"nodeType : {manifest['nodeType']} v{manifest['version']}")
    print(f"contract : {node_json}")
    print(f"code     : {code_tgz}  ({len(bundle)} bytes)")
    print("publish  : kind=satellite language=python  lockfile=<requirements.txt>")


if __name__ == "__main__":
    main()
