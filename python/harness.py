"""Run a satellite node locally for a fast dev loop, before packaging/publishing.

    python harness.py <node_dir> [inputs.json]

Reads `{"inputs": [...], "config": {...}}` from `inputs.json` (or stdin) and prints the
node's raw output. It imports and calls the node in-process — quick, cross-platform, and
enough to check your logic and manifest. It does NOT reproduce the engine's isolation floor
(separate process, rlimits, clean env, no network); that runs for real inside the engine
(`workers/python/isolation.py`). Use `package.py` to produce the publishable artifacts.
"""
import importlib.util
import json
import os
import sys

KIT_DIR = os.path.dirname(os.path.abspath(__file__))


def load_run(node_dir: str):
    sys.path.insert(0, KIT_DIR)
    sys.path.insert(0, node_dir)
    spec = importlib.util.spec_from_file_location("node_main", os.path.join(node_dir, "main.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.run


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    node_dir = os.path.abspath(sys.argv[1])
    raw = open(sys.argv[2], encoding="utf-8").read() if len(sys.argv) > 2 else sys.stdin.read()
    payload = json.loads(raw) if raw.strip() else {"inputs": [], "config": {}}

    run = load_run(node_dir)
    result = run(payload.get("inputs", []), payload.get("config", {}))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
