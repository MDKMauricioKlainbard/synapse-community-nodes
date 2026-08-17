# The node as an arrow — the three-coordinate model (quick reference)

A pocket summary of `Synapse_Node_Model.tex` ("El nodo como flecha", Mauricio Daniel Klainbard).
It is a **reasoning/design model** for the node catalog, not an implementation spec: every node is an
**arrow** `f : A → B`, and three coordinates say *where it points*, *how it reshapes the batch*, and
*how it branches*. Two nodes compose when the arrows line up.

Each bundled/core node declares these under `coordinates` in its manifest; the slugs below are the
exact ones the engine parses.

## 1. World signature (σ) — origin → destination

The kind of data each side of the arrow lives in. Not "what type", but **which world**:

`void` · `table` · `text` · `file` · `image` · `document` · `exotic`

- `void` = no data on that side (a source has `origin: void`; a sink has `destination: void`).
- `exotic` = a world outside the closed set; the catalog raises an alert (nothing ships exotic).

**Composition rule:** two nodes compose when `destination(a) == origin(b)`. This is the primary
"can I wire A into B?" check — the arrows must meet in the same world.

## 2. Cardinality (κ) — how the batch count changes (N : M)

A **structural invariant read from the operation itself**, not a chosen label. It is the relation
between the `N` items in and the `M` items out:

| slug | relation | meaning | example |
|---|---|---|---|
| `source` | `0 : N` | the stream is born | a generator, a reader |
| `preserve` | `N : N` | one in, one out | `sort`, `set` |
| `contract_selective` | `N : M`, `M ≤ N` | drops items | `filter` |
| `contract_total` | `N : 1` | folds the whole batch into one | `reduce`, aggregate |
| `expand` | `N : M`, `M ≥ N` | one item yields many | `split_out` |
| `sink` | `N : 0` | the stream dies | a terminal write |
| `unknown` | — | not statically determined | rare / dynamic |

**Cardinality composes like a multiplier** on the batch size — contract `< 1`, preserve `= 1`,
expand `> 1` — so chaining nodes **multiplies** the factors. That is what makes it a design tool:
you can read a pipeline's batch-size behaviour off the chain, and spot where an `expand` before a
`contract_total` will blow up or collapse.

> Cardinality is *necessary, not always sufficient*: `sort` and `set` share `N:N` and are separated
> by a finer distinction (reorder vs. transform). It is one more coordinate, not the last word.

## 3. Ports — how the node branches

The node's fan structure, independent of the other two:

- `single` — one output port (the common case).
- `fan_out` — several named ports; the engine routes by which received data (`if`, `switch`).
- `fan_in` — several input ports merged into one (`merge`).

The node itself never knows the graph exists; ports are just its declared shape, and the engine
decides what runs next from which ports carried data.

---

## Why three coordinates

World signature alone confuses the `table → table` diagonal (sort, set, filter, reduce all look
identical). Cardinality splits that diagonal. Ports capture branching that neither of the other two
sees. Together they give each node a stable address `⟨origin → destination · cardinality · ports⟩`
that the palette, the graph validator, and the AI assistant all reason with — the assistant composes
by matching worlds and checking cardinality, instead of guessing.

## Addendum: the 4th declared property — `lane` (Phase 3)

Later work added a fourth, orthogonal property: the **data lane** a node exchanges on.

- `row` (default) — N per-item `Struct`s (the historical model). Right for almost everything.
- `columnar` — a single Apache Arrow table (contiguous column buffers), for bulk homogeneous numeric
  data (render grids, ODE solutions, chart data). See `WRITING_A_BUNDLED_NODE.md` §9.

Lane is about *how the payload travels*, not *where the arrow points* — so it sits beside the three
coordinates rather than replacing any of them. The two lanes interoperate: the engine/worker bridges
a row producer into a columnar consumer and vice versa.
