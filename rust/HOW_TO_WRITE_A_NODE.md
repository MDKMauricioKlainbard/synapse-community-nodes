# Writing a WORKFLOW node in Rust (→ WASM)

A Rust node compiles to a **WASM module** that the engine runs in a sandbox: capped memory,
capped CPU (fuel), and **zero** host access (no network, no filesystem, no clock). You write
your node against the [`workflow-node`](workflow-node/) SDK, which hides the guest↔host ABI
entirely. You implement one trait; one macro generates everything the engine calls.

## The four things you write

Open [`examples/uppercase/src/lib.rs`](examples/uppercase/src/lib.rs) — the whole node is
one file. You implement `workflow_node::Node`:

```rust
use workflow_node::{
    export_node, ConfigField, DataField, FieldType, Input, Manifest, Node, NodeError, Output, Value, Widget,
};

#[derive(Default)]
struct Uppercase;

impl Node for Uppercase {
    fn manifest(&self) -> Manifest {           // (1) input contract, (2) output contract, (3) UI
        Manifest::new("uppercase", "Uppercase", "1.0.0")
            .with_config(vec![ConfigField::new(
                "field", FieldType::String, Widget::TextField, false,
                "Name of the field to uppercase (default: text)",
            )])
            .with_input(vec![DataField::new("text", FieldType::String)])
            .with_output(vec![DataField::new("text", FieldType::String)])
    }

    fn run(&self, input: Input) -> Result<Output, NodeError> {   // (4) logic
        let field = input.config_str("field").unwrap_or("text").to_string();
        let out = input.items.into_iter().map(|mut item| {
            if let Some(Value::String(s)) = item.get(&field) {
                if let Some(obj) = item.as_object_mut() {
                    obj.insert(field.clone(), Value::String(s.to_uppercase()));
                }
            }
            item
        }).collect();
        Ok(Output::items(out))
    }
}

export_node!(Uppercase);   // generates the WASM exports the engine calls
```

That's it. No `alloc`, no pointers, no manual JSON.

### 1–2. Contracts (`with_input` / `with_output`)

`DataField`s declare the **fixed shape** of the item data you consume and produce. Declaring
them is what lets the engine notice when your node's output shape doesn't match the next
node's input shape and offer an **AI field-mapping** to bridge the gap. A node whose shape is
dynamic (works on any object) simply declares none — omit `with_input`/`with_output`.

### 3. UI / config (`with_config`)

Each `ConfigField` becomes a form field on the node in the editor — no frontend code. The
`FieldType` says what's stored; the `Widget` says how it's edited (`TextField`, `Checkbox`,
`Select`, …). For a `Select`, your node is the source of truth for the options:

```rust
ConfigField::new("op", FieldType::String, Widget::Select, true, "Comparison")
    .with_options(&[("gte", "≥"), ("lt", "<")])
```

Read config values in `run` via `input.config_str("field")`, `config_f64`, `config_bool`.

### Presentation: icon, description, tags

Make your node legible in the palette with a few chained setters on the manifest:

```rust
Manifest::new("uppercase", "Uppercase", "1.0.0")
    .with_description("Uppercases a chosen text field on every item.")
    .with_icon("type")                       // a curated icon NAME (see below)
    .with_tags(&["transform", "tools", "text"])
```

- **`with_icon`** takes the NAME of a curated icon, not SVG — the frontend maps it to a
  trusted bundled glyph, so a node can never inject markup. Unknown/absent -> a default icon.
  Available names include `type`, `filter`, `pencil`, `git-branch`, `sigma`, `arrow-down-up`,
  `scissors`, `file-text`, `hash`, `activity`, `flask`, `user`, `brackets`, `repeat`,
  `terminal` (see `frontend/src/components/NodeIcon.tsx`; ask to add one if yours is missing).
- **`with_tags`** are free-form categories for grouping + fuzzy search (`"tools"`,
  `"integrations"`, `"science-kit"`, …). A node may carry several.
- **`with_description`** is a one-liner shown as help and indexed by the palette search.

`name` (the second arg to `Manifest::new`) is the display label; `with_display_name` overrides
it only in the rare case you want them different.

### 4. Logic (`run`)

`input.items` is the batch (each item already unwrapped to its JSON data). Return
`Output::items(vec)` for a normal node, or branch to named ports:

```rust
Ok(Output::builder()
    .port("true",  matched)
    .port("false", rest)
    .build())
```

Fail a node cleanly with `Err(NodeError::new("MY_CODE", "why"))` — it surfaces to the
workflow as a node error, distinct from a crash. A per-item shape mismatch is usually **not**
a failure: pass the item through, the way `uppercase` does.

## Build it

```bash
rustup target add wasm32-unknown-unknown          # once
cd examples/uppercase
cargo build --release --target wasm32-unknown-unknown
# -> target/wasm32-unknown-unknown/release/uppercase.wasm
```

Your `Cargo.toml` must declare a cdylib and depend on the SDK:

```toml
[lib]
crate-type = ["cdylib"]

[dependencies]
workflow-node = { path = "../../workflow-node" }
serde_json = "1"
```

## Publish it

Two artifacts come out of your crate:

- **The module** — the `.wasm` above.
- **The contract** — your `manifest()` as JSON. The compiled module exports `describe()`,
  which returns exactly that JSON, so the import tooling reads the contract straight from the
  module (no second file to keep in sync).

Publish (multipart `POST /node-packages`): `kind=wasm`, the `.wasm` as the `wasm` file, and
`contract` set to the manifest JSON. The endpoint compiles the module and checks it honours
the sandbox ABI (`alloc`/`run`/`memory`) **without executing it**, then stores it as
`PENDING_REVIEW`. It runs in workflows only once **approved**.

## The ABI, if you're curious (you don't need this)

The SDK implements the engine's guest ABI (`backend/execution-plane/src/wasm_sandbox.rs`):
exports `alloc(len)->ptr`, `run(ptr,len)->i64` (output pointer in the high 32 bits, length in
the low 32), and `memory`; data crosses as UTF-8 JSON in linear memory. `export_node!`
generates all of it. The one rule it imposes on you: your node struct must be
`Default`-constructible (a unit struct like `struct Uppercase;` is ideal) — the macro builds
a fresh instance per invocation, and your node holds no state between runs (every per-run
value arrives in `Input`).

## Limits

Your guest runs under the FREE tier's caps today (128 MB memory, 10,000,000 fuel). Exceeding
them fails the node with `WASM_MEMORY_LIMIT_EXCEEDED` / `WASM_FUEL_EXHAUSTED`. The guest has
**no** network or filesystem access — a node that needs to call an external API depends on
the credential-injection proxy, which is not yet wired to the WASM ABI (see the roadmap in
`arquitectura_workflow_engine.md`).
