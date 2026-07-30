"""Example community node (Python satellite): **word_count**.

Counts the words in a text field on every input item and adds the count as a new field. It
is the smallest thing that shows the whole shape of a Python node — a contract, a config
field for the UI, and the logic — with no wire-format code.

This file plus a vendored `workflow_node.py` and a `requirements.txt` is the entire node.
Read `nodes/python/HOW_TO_WRITE_A_NODE.md` alongside it.
"""
from workflow_node import BOOL, CHECKBOX, ConfigField, DataField, Manifest, Node, NodeError, Output, NUMBER, STRING, node


class WordCount(Node):
    # (1) input contract, (2) output contract, (3) UI/config — all declared here.
    def manifest(self) -> Manifest:
        return Manifest(
            node_type="word_count",
            name="Word Count",
            version="1.0.0",
            description="Counts the words in a text field on every item.",
            icon="hash",
            tags=["transform", "tools", "text"],
            config_fields=[
                ConfigField(
                    "field", STRING, "text-field", required=False,
                    description="Name of the text field to count (default: text)",
                ),
                ConfigField(
                    "unique_only", BOOL, CHECKBOX, required=False,
                    description="Count only distinct words",
                ),
            ],
            input_fields=[DataField("text", STRING)],
            output_fields=[DataField("text", STRING), DataField("word_count", NUMBER)],
        )

    # (4) the logic. `inputs` is the batch of item data (dicts); `config` is what the user
    # filled into the UI fields above. Return an Output (or a plain list) — never touch the
    # wire format.
    def logic(self, inputs, config):
        field = config.get("field") or "text"
        unique_only = bool(config.get("unique_only"))

        out = []
        for item in inputs:
            value = item.get(field)
            if not isinstance(value, str):
                # A per-item shape mismatch is not a node failure — pass it through with a
                # zero count, the same spirit as the core `filter` node dropping a bad item.
                out.append({**item, "word_count": 0})
                continue
            words = value.split()
            count = len(set(words)) if unique_only else len(words)
            out.append({**item, "word_count": count})
        return Output.items(out)


# Exposes the `run(inputs, config)` entrypoint the satellite worker calls.
run = node(WordCount())
