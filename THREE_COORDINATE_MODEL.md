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

## 4. Lane (λ) — how the payload physically travels (`row` | `columnar`)

A fourth coordinate, **orthogonal** to the three above and **different in kind**. The first three
describe the *logical* shape of the arrow (where it points, how it reshapes the batch, how it
branches). Lane describes the *physical* representation of what rides the wire — and unlike the
first three it is **not merely advisory: its boundary is a security concern.**

- `row` (default) — the batch travels as N per-item `Struct`s, each materialized and re-parsed at
  every node boundary. Right for almost everything: heterogeneous records a node handles one at a
  time.
- `columnar` — the batch travels as **one** Apache Arrow `RecordBatch` (contiguous column buffers).
  Four columns of a million pixels are four typed arrays, not a million dicts. This is the lane for
  bulk homogeneous **numeric** data: render grids, ODE solutions, chart series. See
  `WRITING_A_BUNDLED_NODE.md` §9.

Declared under `lane` in the manifest (absent ⇒ `row`); the engine projects it onto the node
descriptor (`NodeDescriptor.lane`) so the catalog, the canvas badge and the assistant can see it.

### Why the lane boundary is a security frontier, not an optimization

Two `table → table` nodes can sit on **different lanes**. When a **columnar producer** feeds a **row
consumer**, the engine must **materialize** the Arrow buffer — turn its columns into the N individual
items the row consumer expects. This **columnar→row bridge** is where a *bounded* quantity becomes a
*huge* one: a pixel grid is **one** buffer on the columnar lane, but **N items** once it crosses to a
row consumer.

The danger, concretely:

- **One node, one edge, a DoS.** A grid node can emit millions of pixels (a large-but-fine Arrow
  buffer). Wire it into a single row node and the bridge materializes millions of items in engine
  RAM — from one node and one edge.
- **Cardinality (κ) can't see it.** κ anticipates batch explosions *within* a lane (`expand` before
  `contract_total`); this explosion happens *at the crossing between* lanes, invisible to the `N:M`
  axis.
- **Fan-out multiplies it.** One columnar producer feeding `k` row consumers pays the bridge `k`
  times, though each edge alone looks innocent.

So the model treats the first three coordinates as *advisory* (the engine never executes on them) but
the lane as an **enforced** boundary. The design discipline it imposes:

1. **Keep heavy numeric pipelines columnar end-to-end** (grid → compute → colormap → raster stays
   columnar: one buffer, never materialized). columnar↔columnar is the cheap connection.
2. **Cap every columnar→row crossing** — a hard per-crossing row limit; over it the node fails
   explicitly (`COLUMNAR_ROW_BRIDGE_TOO_LARGE`) instead of exhausting RAM. The cap is a safety
   invariant, not a switch you can turn off.
3. **Cap the aggregate too** — because fan-out multiplies, bound the *sum* of what all columnar→row
   crossings in a graph would materialize (charged to the run's bytes-in-flight budget →
   `BYTES_IN_FLIGHT_EXCEEDED`), so a producer fanning out can't slip past the per-edge cap.

Plus an observability obligation: the trace the engine emits to the editor must be volume-independent
(bounded + coalesced), so a fast producer can't flood the channel even while respecting the RAM caps.

**The lane must be declared precisely because its boundary is dangerous:** only if each node exposes
its lane can the palette, the validator and the assistant tell cheap (same-lane) connections from
bridged (capped) ones — and only then can the engine place its defenses on exactly the edges that
need them. The first three coordinates teach us to *compose*; the fourth forces us to *defend*.
