# Writing a WORKFLOW node in Python (satellite)

A Python node runs as a **satellite**: for each invocation the engine spawns a separate,
resource-limited subprocess (`workers/python/isolation.py`) with a clean environment, a
scratch working directory, rlimits, and a wall-clock timeout, then calls one function in your
`main.py`:

```python
def run(inputs, config): ...
```

Use satellites for languages or libraries WASM can't host — e.g. Python with native
extensions like NumPy. You write your node against the [`workflow_node.py`](workflow_node.py)
SDK, a single stdlib-only file you **vendor into your node** so it imports inside the
dependency-free isolation floor.

## The four things you write

Open [`examples/word_count/main.py`](examples/word_count/main.py). You subclass
`workflow_node.Node`:

```python
from workflow_node import ConfigField, DataField, Manifest, Node, Output, node, STRING, NUMBER, BOOL, CHECKBOX

class WordCount(Node):
    def manifest(self):                       # (1) input contract, (2) output contract, (3) UI
        return Manifest(
            node_type="word_count", name="Word Count", version="1.0.0",
            config_fields=[
                ConfigField("field", STRING, "text-field", description="Field to count (default: text)"),
                ConfigField("unique_only", BOOL, CHECKBOX, description="Count distinct words only"),
            ],
            input_fields=[DataField("text", STRING)],
            output_fields=[DataField("text", STRING), DataField("word_count", NUMBER)],
        )

    def logic(self, inputs, config):          # (4) logic
        field = config.get("field") or "text"
        out = []
        for item in inputs:
            value = item.get(field)
            count = len(value.split()) if isinstance(value, str) else 0
            out.append({**item, "word_count": count})
        return Output.items(out)

run = node(WordCount())    # exposes the run(inputs, config) the engine calls
```

The `run = node(WordCount())` line is what turns your class into the `run(inputs, config)`
entrypoint the satellite worker invokes. It also carries your manifest, so the tooling can
read your contract.

### 1–2. Contracts (`input_fields` / `output_fields`)

`DataField`s declare the **fixed shape** of the item data you consume/produce. They let the
engine detect a shape mismatch with a neighbouring node and offer an **AI field-mapping**.
Omit them for a node that works on any object shape.

### Presentation: icon, description, tags

Make your node legible in the palette with a few `Manifest` fields:

```python
Manifest(
    node_type="word_count", name="Word Count", version="1.0.0",
    description="Counts the words in a text field on every item.",
    icon="hash",                          # a curated icon NAME (see below)
    tags=["transform", "tools", "text"],
    ...
)
```

- **`icon`** is the NAME of a curated icon, never SVG — the frontend maps it to a trusted
  bundled glyph, so a node can never inject markup. Unknown/absent -> a default icon.
  Available names include `type`, `filter`, `pencil`, `git-branch`, `sigma`, `arrow-down-up`,
  `scissors`, `file-text`, `hash`, `activity`, `flask`, `user`, `brackets`, `repeat`,
  `terminal` (see `frontend/src/components/NodeIcon.tsx`; ask to add one if yours is missing).
- **`tags`** are free-form categories for grouping + fuzzy search (`"tools"`, `"integrations"`,
  `"science-kit"`, …). A node may carry several.
- **`description`** is a one-liner shown as help and indexed by the palette search.

`name` is the display label; pass `display_name=` only if you want it to differ from `name`.

### 3. UI / config (`config_fields`)

Each `ConfigField` renders as a form field on the node — no frontend code. Field-type and
widget constants are exported by the SDK (`STRING`, `NUMBER`, `BOOL`; `TEXT_FIELD`,
`CHECKBOX`, `SELECT`, …). For a select, your node owns the options:

```python
ConfigField("op", STRING, SELECT, required=True, description="Comparison",
            options=[("gte", "≥"), ("lt", "<")])
```

### 4. Logic (`logic`)

`inputs` is the batch of item data (dicts); `config` is what the user filled into your UI
fields. Return `Output.items([...])` for a normal node, or branch:

```python
return Output.builder().port("true", matched).port("false", rest).build()
```

Raise `NodeError("MY_CODE", "why")` to fail cleanly. (Note: the satellite protocol has no
per-node error-code channel yet, so today the engine reports it as `SATELLITE_NODE_FAILED`
with your message — the WASM runtime already carries per-node codes; parity is on the
roadmap.)

## Your node's dependencies — the lockfile

`requirements.txt` is your node's **pinned-dependency manifest**. The engine derives your
node's satellite **pool** from `hash(language, lockfile)`: every node with the same language
and lockfile shares one dependency environment — that's the reuse the design is built on.
Change a pin and your node moves to a new pool. `word_count` uses only the standard library,
so it pins nothing.

## Test it locally

Fast in-process loop (checks your logic and manifest, not the isolation floor):

```bash
cd nodes/python
echo '{"inputs":[{"text":"one two three"}],"config":{}}' | python harness.py examples/word_count
```

To reproduce the **real** runner (extract bundle, `python -I` isolated import), package it
first (below) and run the engine's satellite worker — that path is exercised by the engine's
own tests (`workers/python/test_isolation.py`).

## Package & publish it

```bash
cd nodes/python
python package.py examples/word_count ./out
# writes out/node.json (the contract) and out/word_count-1.0.0.tar.gz (the code bundle,
# with workflow_node.py vendored in)
```

Publish (multipart `POST /node-packages`):

- `kind=satellite`, `language=python`
- `lockfile` = the contents of your `requirements.txt`
- `code` = the `.tar.gz`
- `contract` = the contents of `node.json`

The endpoint validates the bundle **without executing it** (it must contain a root `main.py`
declaring a top-level `def run(...)`, within size caps) and stores it as `PENDING_REVIEW`. It
runs in workflows only once **approved**.

## What the isolation floor gives (and doesn't)

Per invocation your node gets: a separate process, rlimits (address space / CPU / file size /
fds / procs), a clean env (no engine socket, no credentials), a scratch cwd deleted
afterward, best-effort no-network, and a wall-clock timeout. This **bounds blast radius; it
is not a full sandbox** — the real barrier is the container/microVM tier (deferred) plus the
human review that approved your node in the first place. Because `-I` isolated mode is used,
your node can `import` its own bundled files (auxiliaries and the vendored `workflow_node`),
but nothing outside the bundle.
