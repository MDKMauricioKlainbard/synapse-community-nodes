"""workflow_node — write a WORKFLOW community node in Python.

A Python community node runs as a **satellite**: a separate, resource-limited subprocess
the engine spawns per invocation (`workers/python/isolation.py`) with a clean environment,
a scratch working directory, rlimits and a wall-clock timeout. The engine hands your node a
batch over the ABI and calls one function:

    def run(inputs, config) -> list | {"ports": {...}}

That is the whole contract. This module gives you the ergonomics around it so you write
only the four things that matter — input contract, output contract, UI/config, and logic —
and never touch the wire shape by hand.

## How to use it

Vendor this single file into your node bundle (it is stdlib-only, so it works inside the
dependency-free isolation floor) and, in `main.py`:

    from workflow_node import Node, Manifest, ConfigField, DataField, Output, NodeError, node

    class WordCount(Node):
        def manifest(self):
            return Manifest("word_count", "Word Count", "1.0.0", ...)

        def logic(self, inputs, config):
            ...
            return Output.items([...])          # or Output.builder().port("a", [...]).build()

    run = node(WordCount())   # exposes the `run(inputs, config)` the engine calls

`node(...)` adapts your class to the raw ABI: it normalizes your `Output` (or a plain list)
into the shape the worker expects, and lets a `NodeError` fail the node cleanly.

`python -m workflow_node manifest main.py` prints the node's manifest JSON (what you upload
as the publish `contract`) by importing your module and reading `run.manifest`.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional, Union

# ---------------------------------------------------------------------------------------
# Field / manifest types — the contract + UI, declared in code (single source of truth).
# Serialize to the exact JSON the engine's node catalog / publish `contract` expects.
# ---------------------------------------------------------------------------------------

# Field types (mirror the engine's ConfigFieldType). Plain strings — the wire values.
STRING = "string"
NUMBER = "number"
BOOL = "bool"
OBJECT = "object"
LIST = "list"

# Widgets (mirror the engine's Widget): how the UI renders a config field.
TEXT_FIELD = "text-field"
TEXT_AREA = "text-area"
NUMBER_FIELD = "number-field"
CHECKBOX = "checkbox"
SELECT = "select"
KEY_VALUE_LIST = "key-value-list"
LIST_EDITOR = "list-editor"
# A metadata-driven TABLE of homogeneous rows (chart series, param sweeps): one row per element of
# an owned LIST field, columns declared in this field's `widget_metadata` (see ConfigField). Unlike
# the bespoke switch-routes/document-composer widgets whose shape is fixed, its columns are
# node-specific, so the node ships them in the manifest.
RECORD_TABLE = "record-table"
# The three the Python SDK could not declare until 2026-07-28 — a Python node had no way to
# ask for a credential picker or a file picker, which meant the LANGUAGE decided what UI a
# node could have. It should not.
CREDENTIAL_SELECT = "credential-select"
FILE_UPLOAD = "file-upload"
SCRIPT = "script"                    # a Rhai code editor
PYTHON_SCRIPT = "python-script"      # a Python code editor, run on a satellite tier (like python_code)
# Widgets that know something the bare field does not.
FIELD_SELECTOR = "field-selector"    # combobox over the fields arriving from upstream
MULTI_SELECT = "multi-select"        # several values, stored as a list (not a CSV string)
JSON_EDITOR = "json-editor"          # object/list edited as JSON, validated live
REGEX_TESTER = "regex-tester"        # regex + a sample to try it against
TIMEZONE_SELECT = "timezone-select"  # IANA zones, from the browser's own ICU data
DATE_PICKER = "date-picker"          # ISO-8601 date/time
RANGE = "range"                      # a numeric {start, end} in one row


class ConfigField:
    """One field in your node's UI / config. `options` is required-non-empty only for a
    SELECT widget; the node is the source of truth for them, never the frontend."""

    def __init__(
        self,
        name: str,
        field_type: str,
        widget: str,
        required: bool = False,
        description: str = "",
        options: Optional[List[tuple]] = None,
        min: Optional[float] = None,
        max: Optional[float] = None,
        pattern: Optional[str] = None,
        max_length: Optional[int] = None,
        default: Any = None,
        show_when: Optional[List["ShowWhen"]] = None,
        template_code: str = "",
        widget_metadata: Optional[Dict[str, Any]] = None,
    ):
        self.name = name
        self.field_type = field_type
        self.widget = widget
        self.required = required
        self.description = description
        self.options = options or []
        self.min = min
        self.max = max
        self.pattern = pattern
        self.max_length = max_length
        # What a NEW instance starts with. Authoring-time: the editor seeds it, the engine
        # does not — your `logic` must still handle the key being absent.
        self.default = default
        # Show the field only while every condition holds (AND). Empty = always.
        self.show_when = show_when or []
        # Starter code a code-editor widget (SCRIPT / PYTHON_SCRIPT) is PRE-LOADED with on a fresh
        # instance. Unlike `default`, it is scaffolding the user edits: it states the script's
        # contract (the variables in scope, the shape to return, a required function name). Empty
        # ⇒ none. The language is implied by the widget, so this is just the code.
        self.template_code = template_code
        # PER-FIELD widget metadata (Bloque 5) — the rich contract a composite widget needs and the
        # bare field cannot express. For a `record-table` widget it declares `ownsFields` (the list
        # field it edits) plus the `columns` (each {name, label, widget, options?, default?}). Fixed
        # widgets (condition-builder/switch-routes) compute theirs from the widget type; this is for
        # widgets whose shape is NODE-specific. Emitted verbatim; the frontend reads it to render.
        self.widget_metadata = widget_metadata or {}

    def to_json(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "name": self.name,
            "fieldType": self.field_type,
            "widget": self.widget,
            "required": self.required,
            "description": self.description,
        }
        if self.options:
            out["options"] = [{"value": v, "label": l} for v, l in self.options]
        if self.min is not None:
            out["min"] = self.min
        if self.max is not None:
            out["max"] = self.max
        if self.pattern is not None:
            out["pattern"] = self.pattern
        if self.max_length is not None:
            out["maxLength"] = self.max_length
        # `is not None`, not truthiness: `default=False` and `default=0` are real defaults,
        # and dropping them is exactly the bug that forced `csv_build` to expose `no_header`.
        if self.default is not None:
            out["default"] = self.default
        if self.show_when:
            out["showWhen"] = [c.to_json() for c in self.show_when]
        if self.template_code:
            out["templateCode"] = self.template_code
        if self.widget_metadata:
            out["widgetMetadata"] = self.widget_metadata
        return out


class ShowWhen:
    """Show a config field only while a sibling field holds one of these values.

    The n8n `displayOptions` idea: a node with modes should show the fields of the mode you
    picked, not of every mode at once. Values, not expressions — "which mode is selected"
    is what every real case needs, and a condition language would have to be implemented
    identically in the engine, three SDKs and the editor."""

    def __init__(self, field: str, equals: List[Any]):
        self.field = field
        self.equals = equals

    def to_json(self) -> Dict[str, Any]:
        return {"field": self.field, "equals": self.equals}


class DataField:
    """One field of your node's input or output contract — the fixed shape of item data
    you consume/produce. Declaring these lets the engine detect an edge mismatch and
    propose an AI field-mapping. Omit them for a dynamic (any-shape) node."""

    def __init__(self, name: str, field_type: str):
        self.name = name
        self.field_type = field_type

    def to_json(self) -> Dict[str, Any]:
        return {"name": self.name, "fieldType": self.field_type}


class Manifest:
    """Everything the engine needs to catalog and wire your node, declared in code so the
    contract and the logic never drift into two files. `to_json()` is the publish
    `contract` payload (plus the identity fields the publish request also carries)."""

    def __init__(
        self,
        node_type: str,
        name: str,
        version: str,
        config_fields: Optional[List[ConfigField]] = None,
        input_fields: Optional[List[DataField]] = None,
        output_fields: Optional[List[DataField]] = None,
        description: str = "",
        icon: str = "",
        tags: Optional[List[str]] = None,
        display_name: str = "",
        tier: str = "",
        category: str = "",
        provider: str = "",
        capabilities: Optional[List[str]] = None,
        ai_usage: str = "",
        coordinates: Optional[Dict[str, str]] = None,
        streaming: bool = False,
        production_mode_configurable: bool = False,
    ):
        self.node_type = node_type
        self.name = name
        self.version = version
        self.config_fields = config_fields or []
        self.input_fields = input_fields or []
        self.output_fields = output_fields or []
        # The curated dependency environment this node needs, `name@version` (e.g.
        # "python-data-science@1.0.0"). A satellite node declares its ENVIRONMENT by tier name,
        # not by carrying a hand-written lockfile: the build resolves the tier to its exact
        # pinned lockfile (backend/execution-plane/tiers/), so there is one source of truth per
        # environment and an author cannot mint a new pool by choosing their own versions.
        # Empty = the node carries its own lockfile the old way (legacy / pre-tier nodes).
        self.tier = tier
        # Presentation metadata: how the node reads in the palette/card. Cosmetic only.
        self.description = description
        # A curated icon NAME (the frontend maps it to a trusted inline SVG — never raw
        # markup). Empty -> the frontend's default icon.
        self.icon = icon
        # Free-form tags for grouping/search: "tools", "integrations", "science-kit", …
        self.tags = tags or []
        # A palette label distinct from `name` (rarely needed).
        self.display_name = display_name
        # The node's PRIMARY category — one of the server's controlled slugs (triggers-inputs,
        # flow-control, data-transformation, files-documents, ai-ml, integrations, developer-tools,
        # outputs). The SERVER is the authority: a value outside its list degrades to `other`, and
        # the category is never inferred from tags[0]. Empty = let the server decide (`other`).
        self.category = category
        # The product/ecosystem this node belongs to ("Slack", "Google") — not the author. A facet
        # and a search field. Empty = none.
        self.provider = provider
        # Coarse capability slugs the node advertises ("http", "streaming", "file-output"), for
        # faceting/search. Empty = none.
        self.capabilities = capabilities or []
        # A concise usage note written FOR AN AI ASSISTANT (distinct from `description`, the user's
        # tooltip): how the node wires, the rule easy to get wrong, when to pick another node. The
        # assistant reads it to use the node right the first time. Advisory only. Empty = none.
        self.ai_usage = ai_usage
        # The node's three INTERACTION COORDINATES ("El nodo como flecha") — a dict
        # {origin, destination, cardinality, ports} with the engine's stable slugs. MANDATORY for a
        # real node: leaving a world "exotic" (or omitting the dict) trips the catalog's exotic-world
        # alert. Worlds: void/table/text/file/image/document. Cardinality:
        # source/preserve/contract_selective/contract_total/expand/sink/unknown. Ports:
        # single/fan_out/fan_in. Two nodes compose when destination(a) == origin(b).
        self.coordinates = coordinates or {}
        # STREAMING PRODUCTION (Slice 8): set True when this node's `logic` PRODUCES ITS OUTPUT
        # INCREMENTALLY — it `yield`s items/chunks instead of returning a full list. The engine
        # reads this to dispatch the node through the streaming path (result-chunk frames, bounded
        # memory, pipeline overlap) rather than collecting one big batch. Only meaningful for a
        # satellite node; a batch node leaves it False. The node's actual return type must match
        # this declaration — the worker checks and fails a mismatch explicitly.
        self.streaming = streaming
        # PRODUCTION MODE (§11.5): when True, the user may retune how this node's STREAMED output is
        # grouped downstream via the reserved `_production` override (per-item / chunk / batch),
        # exactly like the core `csv_stream`. Only meaningful for a streaming node; a node that does
        # not opt in ignores any `_production`. Left False by default (production is the node's own
        # concern, opened to the user only where the node says it is safe).
        self.production_mode_configurable = production_mode_configurable

    def to_json(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "nodeType": self.node_type,
            "name": self.name,
            "version": self.version,
            "configFields": [f.to_json() for f in self.config_fields],
            "inputFields": [f.to_json() for f in self.input_fields],
            "outputFields": [f.to_json() for f in self.output_fields],
        }
        # Only emit presentation fields when set, so the contract stays minimal.
        if self.display_name:
            out["displayName"] = self.display_name
        if self.description:
            out["description"] = self.description
        if self.icon:
            out["icon"] = self.icon
        if self.tags:
            out["tags"] = list(self.tags)
        # Catalog-organization metadata (node-catalog redesign) — declared, only emitted when set,
        # so the contract stays minimal. The control-plane validates `category` against its
        # controlled list (unknown -> `other`) and derives origin/verified itself; these are
        # discovery hints, never trust inputs.
        if self.category:
            out["category"] = self.category
        if self.provider:
            out["provider"] = self.provider
        if self.capabilities:
            out["capabilities"] = list(self.capabilities)
        if self.ai_usage:
            out["aiUsage"] = self.ai_usage
        # The three interaction coordinates ("El nodo como flecha"), emitted as-is; pack.py copies
        # them into node.json and the engine ships them to the assistant so it composes by matching
        # worlds. Only emitted when declared.
        if self.coordinates:
            out["coordinates"] = dict(self.coordinates)
        # The tier is emitted so the packer/publisher can resolve it to a lockfile. It is not
        # part of the catalog descriptor the engine renders — it is packaging metadata, the same
        # category as `runtime`/`entrypoint`/`lockfile`.
        if self.tier:
            out["tier"] = self.tier
        # Streaming production (Slice 8): only emitted when True, so a batch node's contract is
        # unchanged. The engine's bundled-node loader reads it onto `BundledNode.streaming`.
        if self.streaming:
            out["streaming"] = True
        # Production-mode opt-in (§11.5): lets the user retune a streaming node's emission grouping.
        if self.production_mode_configurable:
            out["productionModeConfigurable"] = True
        return out


# ---------------------------------------------------------------------------------------
# What an author works with in logic: Output and NodeError.
# ---------------------------------------------------------------------------------------

# An item's data is a plain JSON object (dict). Kept as a name for readable signatures.
Item = Dict[str, Any]


class InputFile:
    """A file attached to an incoming item, already written to your working directory.

    `path` is a real local path you can `open()` — the worker fetched the bytes from the
    engine's storage before your node started, because your process deliberately cannot
    reach the engine itself. `filename` is what a human called it; do NOT use it as a path
    (it may contain anything), use `path`."""

    def __init__(self, path: str, filename: str, mime_type: str, size_bytes: int):
        self.path = path
        self.filename = filename
        self.mime_type = mime_type
        self.size_bytes = size_bytes

    def read(self) -> bytes:
        with open(self.path, "rb") as handle:
            return handle.read()

    def __repr__(self) -> str:
        return f"InputFile({self.filename!r}, {self.size_bytes} bytes)"


class Output:
    """Your node's result: items keyed by output port. Most nodes have a single output —
    build those with `Output.items([...])`. A branching node emits several named ports via
    `Output.builder().port("true", [...]).port("false", [...]).build()`.

    A node that PRODUCES a file writes it into the working directory and declares it with
    `.with_files([...])`. Declaring is required: the worker uploads exactly what you name,
    so a scratch or intermediate file you happened to write never becomes an artifact by
    accident."""

    DEFAULT_PORT = "default"

    def __init__(self, ports: Dict[str, List[Item]], files: Optional[List[str]] = None):
        self._ports = ports
        self._files = list(files or [])

    @classmethod
    def items(cls, items: List[Item]) -> "Output":
        """The single-output case: these items go out the `default` port."""
        return cls({cls.DEFAULT_PORT: list(items)})

    def with_files(self, paths: List[str]) -> "Output":
        """Declare files you wrote (paths relative to your working directory) as this
        node's output. They are stored by the engine and attached to the emitted items."""
        self._files = list(paths)
        return self

    @classmethod
    def builder(cls) -> "OutputBuilder":
        return OutputBuilder()

    def to_raw(self) -> Dict[str, Any]:
        """The shape the satellite worker consumes: `{"ports": {port: [items]}, "files": [...]}`."""
        raw: Dict[str, Any] = {"ports": self._ports}
        if self._files:
            raw["files"] = self._files
        return raw


class OutputBuilder:
    def __init__(self):
        self._ports: Dict[str, List[Item]] = {}
        self._files: List[str] = []

    def port(self, name: str, items: List[Item]) -> "OutputBuilder":
        self._ports[name] = list(items)
        return self

    def files(self, paths: List[str]) -> "OutputBuilder":
        self._files = list(paths)
        return self

    def build(self) -> Output:
        return Output(self._ports, self._files)


class NodeError(Exception):
    """A clean node failure — your node reporting "I can't process this". Raise it from
    `logic`. The satellite worker surfaces it to the workflow as a NodeError.

    NOTE: the satellite protocol has no per-node error-code channel yet (unlike the WASM
    ABI's `{"error": {...}}`), so today the engine reports this as code
    `SATELLITE_NODE_FAILED` with your message. `code` is carried in the message and is
    forward-looking for when that channel exists."""

    def __init__(self, code: str, message: str):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------------------
# The base class an author implements, and the adapter to the raw ABI.
# ---------------------------------------------------------------------------------------


class Node:
    """Subclass this, implement `manifest` and `logic`, and expose
    `run = node(YourNode())` at module top level. The four methods map one-to-one to the
    four things a node author declares; nothing about the wire format appears here."""

    def manifest(self) -> Manifest:
        raise NotImplementedError("a node must declare its manifest()")

    def logic(self, inputs: List[Item], config: Dict[str, Any]) -> Union[Output, List[Item]]:
        """Your logic: consume the input batch + config, return an `Output` (or, for the
        single-port case, a plain list of items). Raise `NodeError` to fail cleanly.

        STREAMING (Slice 8): to produce incrementally, make `logic` a GENERATOR — `yield` an
        `Output` (or a list) per chunk instead of returning one. The engine then streams your
        output downstream frame by frame, with bounded memory and backpressure (the `yield`
        blocks while the engine is busy). Declare `streaming=True` in your `Manifest` so the
        engine dispatches you through the streaming path; the two MUST agree."""
        raise NotImplementedError("a node must implement logic()")


def node(instance: Node) -> Callable[[List[Item], Dict[str, Any]], Any]:
    """Adapt a `Node` to the module-level `run(inputs, config)` the satellite worker calls.
    Normalizes the return: an `Output` -> its raw ports; a plain list -> the default port.
    A `NodeError` propagates (the worker fails the node); anything else is your logic's
    responsibility. Exposes `.manifest` for the `python -m workflow_node manifest` tool."""

    def run(inputs: List[Item], config: Dict[str, Any], files: Any = None) -> Any:
        # `files` is passed ONLY to a logic() that declares it. Introspection rather than a
        # required parameter, so every node written before files existed keeps working
        # unchanged — the overwhelming majority of nodes never touch a file, and making
        # them all take an argument they ignore would be a worse contract.
        import inspect

        takes_files = "files" in inspect.signature(instance.logic).parameters
        if takes_files:
            # The worker sends plain dicts over the process boundary (it is JSON on stdin).
            # They are wrapped into `InputFile` HERE, at the SDK edge, so a node author gets
            # an object with `.read()` and `.path` rather than having to remember key names.
            wrapped = [
                InputFile(
                    path=f["path"],
                    filename=f.get("filename", ""),
                    mime_type=f.get("mime_type", "application/octet-stream"),
                    size_bytes=f.get("size_bytes", 0),
                )
                for f in (files or [])
            ]
            result = instance.logic(inputs or [], config or {}, wrapped)
        else:
            result = instance.logic(inputs or [], config or {})
        if isinstance(result, Output):
            return result.to_raw()
        if isinstance(result, list):
            return result
        # STREAMING (Slice 8): a `logic` that `yield`s produces a GENERATOR here instead of a
        # single Output. Return a generator of RAW FRAMES (each the same `{"ports": {...}}` shape a
        # batch result has); the worker sends one `result_chunk` per frame and the engine consumes
        # them incrementally. The node declares `streaming=True` in its manifest so the engine sets
        # up the streaming dispatch; the two must agree, which is why a mismatch is made explicit at
        # the worker/runner boundary rather than guessed at here.
        if inspect.isgenerator(result):
            return _normalize_frames(result)
        raise NodeError(
            "NODE_BAD_OUTPUT",
            "logic() must return an Output or a list of items (or yield them, for a streaming node)",
        )

    run.manifest = instance.manifest()  # type: ignore[attr-defined]
    return run


def _normalize_frames(generator: Any):
    """Adapts a streaming `logic` generator (which yields `Output`s or item lists) into a
    generator of RAW frames the worker serializes — one per yield. Kept lazy: each frame is
    normalized as it is pulled, so the node's `yield` and the wire stay in lockstep (which is
    what lets backpressure reach the node)."""
    for chunk in generator:
        if isinstance(chunk, Output):
            yield chunk.to_raw()
        elif isinstance(chunk, list):
            yield {"ports": {Output.DEFAULT_PORT: chunk}}
        else:
            raise NodeError(
                "NODE_BAD_OUTPUT",
                "a streaming logic() must yield an Output or a list of items",
            )


def _emit_manifest(main_path: str) -> str:
    """Import a node's main.py and return its manifest JSON — the publish `contract`."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("node_main", main_path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load a module from {main_path!r}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    run = getattr(mod, "run", None)
    manifest = getattr(run, "manifest", None)
    if manifest is None:
        raise SystemExit(
            f"{main_path} has no `run = node(...)` at top level, so it declares no manifest"
        )
    return json.dumps(manifest.to_json(), indent=2)


if __name__ == "__main__":
    import sys

    if len(sys.argv) == 3 and sys.argv[1] == "manifest":
        print(_emit_manifest(sys.argv[2]))
    else:
        print("usage: python -m workflow_node manifest <path-to-main.py>", file=sys.stderr)
        sys.exit(2)
