# Data flow between nodes — write nodes that compose naturally

The node contract is fixed and small: a node takes a **batch of items + its config**, and
returns **items per output port**. That never changes. What *does* determine whether nodes
chain together pleasantly — or fight the user at every edge — is two conventions in how you
write the logic. Both are choices inside the existing contract, not new contract features.

Follow them and a node stops being a rigid "expects exactly these fields" box and becomes a
step that slots into an existing pipeline without forcing rename/mapping nodes between hops.

## 1. Passthrough by default — operate and augment, don't replace

A node should touch the fields it cares about and **leave every other field on the item
intact.** It augments the item; it does not rebuild it from scratch.

Why it matters: workflows are pipelines. If each node returns *only* the fields it produces,
every downstream node loses the context an upstream node attached (an id, a timestamp, the
original text). Users then need extra nodes just to carry data across. Passthrough makes a
chain of ten nodes accumulate data instead of shedding it.

```python
# GOOD — augments each item, keeps the rest
def logic(self, inputs, config):
    return Output.items([{**item, "word_count": count(item)} for item in inputs])

# BAD — drops everything the item already carried
def logic(self, inputs, config):
    return Output.items([{"word_count": count(item)} for item in inputs])
```

```rust
// GOOD — mutate the field, return the whole item
let out = input.items.into_iter().map(|mut item| {
    if let Some(obj) = item.as_object_mut() { obj.insert("upper".into(), /* … */); }
    item
}).collect();
```

Both example nodes do this: `word_count` returns `{**item, "word_count": n}`, `uppercase`
overwrites one field and returns the item unchanged otherwise. A per-item shape mismatch
(the field is missing or the wrong type) is **not** a node failure — pass that item through
untouched, the way the core `filter` node drops a non-matching item rather than erroring.

## 2. Field-reference config — read the field the user names, don't demand a fixed input

Instead of hard-coding "my input must have a field called `value`", let the node take a
**config field that names which input field to read.** The same node then works on any
upstream shape — the user points it at the right field instead of inserting a rename node.

```python
def logic(self, inputs, config):
    field = config.get("field") or "value"      # the user says which field
    for item in inputs:
        v = item.get(field)
        ...
```

Declare that config field in your manifest like any other. Today it renders as a text field
where the user types the field name; a dedicated **field-selector** widget (a picker driven
by the upstream node's output schema) is a planned UI upgrade — declaring the field now means
you get that picker for free when it lands, with no contract change.

`zscore_outliers` uses this: its `field` config selects the numeric field to analyze, so it
runs against *any* items that carry a number, not only ones shaped exactly how it expects.

## When fixed shapes are still the right call

Declaring fixed `input_fields` / `output_fields` is not wrong — it is what lets the engine
detect an edge mismatch and offer an **AI field-mapping**. Use fixed shapes when your node
genuinely models a specific record (the `person` / `contact` example pair). Use the two
conventions above when your node is a general transform that should adapt to whatever flows
in. Most **action** nodes are the latter: prefer passthrough + a field-reference config, and
your node will feel far less restrictive without giving up a single guarantee of the contract.
