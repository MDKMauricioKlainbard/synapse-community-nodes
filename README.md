# `nodes/` — community node authoring kit

This directory is where **community nodes** live and where you learn to write one. A
community node is a reusable step a workflow can drop onto its canvas — an "uppercase this
field", a "count words", a "call the Acme API" — contributed by someone outside the core
team.

You write **four things and nothing else**:

1. **Input contract** — the shape of the item data your node consumes.
2. **Output contract** — the shape it produces.
3. **UI / config** — the fields the user fills in on the node (rendered automatically).
4. **Logic** — what your node actually does.

Everything else — how the engine calls your code, how data crosses the sandbox boundary,
memory, JSON marshalling — is handled by a small SDK per language. You never touch it.

## Pick your language

| Language | Runtime | How it runs | Start here |
|---|---|---|---|
| **Rust** (and anything compiling to `wasm32`) | **WASM sandbox** | Compiled to a `.wasm` module, run inside `wasmtime` with capped memory/CPU and **zero** host access. Runs in the shipped product. | [`rust/HOW_TO_WRITE_A_NODE.md`](rust/HOW_TO_WRITE_A_NODE.md) |
| **Python** (and other non-WASM languages) | **Satellite worker** | Run as a separate, resource-limited subprocess per invocation. For languages with native libraries WASM can't host. | [`python/HOW_TO_WRITE_A_NODE.md`](python/HOW_TO_WRITE_A_NODE.md) |

Both runtimes speak the **same node contract**: a node receives a batch of items plus its
config, and returns items grouped by output port (`default` for a normal node; named ports
for a brancher like an `if`). Only the delivery mechanism differs.

Read [`DATA_FLOW.md`](DATA_FLOW.md) too: two conventions (passthrough-by-default and
field-reference config) that make a node compose naturally in a pipeline instead of forcing
rename/mapping nodes between every hop — without changing the contract.

```
nodes/
  rust/
    workflow-node/          the Rust SDK crate (hides the WASM ABI)
    examples/uppercase/     a complete, buildable example node
    HOW_TO_WRITE_A_NODE.md
  python/
    workflow_node.py        the Python SDK (a single stdlib-only file you vendor)
    examples/word_count/     a complete example node
    package.py              build the publishable artifacts (bundle + contract)
    harness.py              run a node locally for a fast dev loop
    HOW_TO_WRITE_A_NODE.md
```

## How a node reaches production (the trust path)

This project **never executes third-party code that has not been reviewed.** The trust model
has two parts — one **built**, one **planned** — and this section is careful to say which is
which (an earlier version of this file described the planned repo/CI as if it already existed;
it does not — see CAMINO_A_PRODUCCION §8).

**Built today — the approval governance (the part that grants trust).** Publishing a node
version records it as `PENDING_REVIEW`; it **cannot run** until an **ADMIN** approves it. The
gate is real: role on `User`, `POST /node-packages/versions/:id/approve|reject` behind a
`RolesGuard` (non-admin → 403), an audit trail (`reviewedBy`/`reviewedAt`), and a runtime that
**refuses** to dispatch a non-`APPROVED` version (it fails closed with `NODE_NOT_APPROVED` /
`SATELLITE_NOT_APPROVED`, never silently substitutes echo). So the *decision to trust* a node,
and the *enforcement* of that decision at run time, both exist (SECURITY_LOG #10).

**Planned — the community submission pipeline (how a node gets to that gate).** A separate,
isolated **community repository** where a non-core contributor proposes a node, a CI check
validates the bundle (the same structural checks the publish endpoint runs, `WebAssembly.Module`
/ `assertSatelliteCode`), and — after review — it is submitted to the main registry for an ADMIN
to approve. A `submit` CLI so an author does not assemble the multipart `POST /node-packages` by
hand, and publish/review **UIs**. This is **not built yet** (CAMINO §8.4, Track 5); today a node
is published by calling `POST /node-packages` directly, and reviewed via the approve/reject
endpoints above.

Either way, the SDK and the sandbox/isolation floor are a **backstop**, not the primary security
boundary — the review is. The sandbox (WASM) and the per-invocation isolation (satellite) bound
what a mistake or a compromised dependency can do; they do not replace the human decision to
trust a node. See `SECURITY_LOG.md`.

## What is NOT here

The engine's **core nodes** (`echo`, `switch`, `set`, `filter`, `person`, …) live in
`backend/execution-plane/src/nodes/`. Those are first-party, trusted, compiled **into** the
engine and run in-process with no sandbox — a different trust tier entirely. They are not
community nodes and are not authored with this kit. If you are a core-team contributor
adding a built-in node, follow the `Node` trait in that directory instead.
