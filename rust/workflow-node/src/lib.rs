//! # `workflow-node` — write a WORKFLOW community node in Rust
//!
//! A community node runs as a **sandboxed WASM module** inside the engine
//! (`backend/execution-plane/src/wasm_sandbox.rs`): capped memory, capped CPU (fuel),
//! zero host imports. The engine talks to a guest over a tiny ABI — `alloc` / `run` /
//! `memory`, JSON marshalled by hand over linear memory, a packed `i64` return. Writing
//! that by hand is error-prone and has nothing to do with your node's job.
//!
//! **This crate hides all of it.** You implement one trait — [`Node`] — declaring four
//! things and nothing else:
//!
//! 1. your **input contract** ([`Node::manifest`] → `input_fields`),
//! 2. your **output contract** (`output_fields`),
//! 3. your **UI / config** (`config_fields`),
//! 4. your **logic** ([`Node::run`]).
//!
//! Then one line — [`export_node!`] — generates the ABI. See
//! [`../HOW_TO_WRITE_A_NODE.md`](https://example/) and the `examples/uppercase` crate.
//!
//! ## The ABI this crate implements (so you don't have to)
//!
//! Kept byte-for-byte in step with the engine's sandbox:
//! - Input:  `{ "items": [ { "json": <object|null> }, … ], "config": <value|null> }`
//! - Output: `{ "ports": { "<port>": [ { "json": <object|null> }, … ] } }`
//!           or `{ "error": { "code": "<STR>", "message": "<STR>" } }`
//! - Exports: `alloc(len:i32)->i32`, `run(ptr:i32,len:i32)->i64` (ptr in the high 32 bits,
//!   len in the low 32), `memory`. Plus `describe()->i64`, which returns this node's
//!   manifest JSON over the same ABI, so the node-import tooling can read the contract
//!   straight from the compiled module.

use std::collections::BTreeMap;

use serde::Serialize;
pub use serde_json::{json, Value};

// ---------------------------------------------------------------------------------------
// What an author works with: Input, Output, NodeError.
// ---------------------------------------------------------------------------------------

/// The batch handed to your node: the input items' data and this instance's config.
///
/// `items` are already unwrapped to each item's `json` payload (the common case — you work
/// with the data, not the wire envelope). `config` is the values the user filled into your
/// UI fields, or `Value::Null` when the node has no config.
pub struct Input {
    pub items: Vec<Value>,
    pub config: Value,
}

impl Input {
    /// A config field as a `&str`, or `None` if unset / not a string. Convenience over
    /// `self.config.get(name)`.
    pub fn config_str<'a>(&'a self, name: &str) -> Option<&'a str> {
        self.config.get(name).and_then(Value::as_str)
    }

    /// A config field as an `f64`, or `None` if unset / not a number.
    pub fn config_f64(&self, name: &str) -> Option<f64> {
        self.config.get(name).and_then(Value::as_f64)
    }

    /// A config field as a `bool`, or `None` if unset / not a bool.
    pub fn config_bool(&self, name: &str) -> Option<bool> {
        self.config.get(name).and_then(Value::as_bool)
    }
}

/// Your node's result: items keyed by output port. Most nodes have a single output —
/// build those with [`Output::items`] (or `Vec<Value>` via `.into()`). A branching node
/// (an `if`, a `switch`) emits to several named ports with [`Output::builder`].
#[derive(Default)]
pub struct Output {
    ports: BTreeMap<String, Vec<Value>>,
}

/// The port every non-branching node emits on. The engine's [`DEFAULT_PORT`] equivalent.
pub const DEFAULT_PORT: &str = "default";

impl Output {
    /// The single-output case: these items go out the `default` port.
    pub fn items(items: Vec<Value>) -> Self {
        let mut ports = BTreeMap::new();
        ports.insert(DEFAULT_PORT.to_string(), items);
        Self { ports }
    }

    /// Start building a multi-port output (a branching node).
    pub fn builder() -> OutputBuilder {
        OutputBuilder { ports: BTreeMap::new() }
    }
}

impl From<Vec<Value>> for Output {
    fn from(items: Vec<Value>) -> Self {
        Output::items(items)
    }
}

/// Builds an [`Output`] with explicitly named ports.
pub struct OutputBuilder {
    ports: BTreeMap<String, Vec<Value>>,
}

impl OutputBuilder {
    /// Add a named port and the items routed to it.
    pub fn port(mut self, name: &str, items: Vec<Value>) -> Self {
        self.ports.insert(name.to_string(), items);
        self
    }

    pub fn build(self) -> Output {
        Output { ports: self.ports }
    }
}

/// A clean node failure — your node reporting "I can't process this", distinct from a
/// crash or a resource-limit trap. Surfaces to the workflow as a `NodeError` with your
/// `code` and `message`, exactly like a core node's error.
pub struct NodeError {
    pub code: String,
    pub message: String,
}

impl NodeError {
    pub fn new(code: impl Into<String>, message: impl Into<String>) -> Self {
        Self { code: code.into(), message: message.into() }
    }
}

// ---------------------------------------------------------------------------------------
// The manifest: the contract + UI, declared in code (single source of truth). Serializes
// to the exact JSON the engine's node catalog / publish `contract` expects.
// ---------------------------------------------------------------------------------------

/// The declared type of a config or data field — enough for the UI to pick a widget and
/// for basic validation. Mirrors the engine's `ConfigFieldType`.
#[derive(Clone, Copy, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum FieldType {
    String,
    Number,
    Bool,
    Object,
    List,
}

/// How the UI renders a config field. Orthogonal to [`FieldType`] (the type says WHAT is
/// stored, the widget says HOW to edit it). Mirrors the engine's `Widget`.
#[derive(Clone, Copy, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum Widget {
    TextField,
    TextArea,
    NumberField,
    Checkbox,
    Select,
    KeyValueList,
    ListEditor,
    // Until 2026-07-28 the list stopped above, so a node written in Rust could not ask for a
    // credential picker or a file picker that the engine has rendered all along. The SDKs
    // were the limit, not the product: which UI a node can have must not depend on which
    // language it happens to be written in.
    CredentialSelect,
    FileUpload,
    Script,
    /// Combobox over the fields arriving from upstream: suggestions when the shape is
    /// known, free text when it is dynamic. Never a closed dropdown — half the catalogue
    /// declares no output schema, and that is exactly where naming a field is hardest.
    FieldSelector,
    /// Several values, stored as a list rather than a comma-separated string.
    MultiSelect,
    /// Object/list edited as JSON, validated live.
    JsonEditor,
    /// A regex plus a sample to try it against, before the first run does it for you.
    RegexTester,
    /// IANA time zones, from the browser's own ICU data.
    TimezoneSelect,
    /// ISO-8601 date/time.
    DatePicker,
}

/// A literal config value: what a field DEFAULTS to, and what a [`ShowWhen`] compares
/// against. Scalars only — a default is a starting point a human edits, and a condition is
/// "which mode is selected".
#[derive(Clone, Serialize)]
#[serde(untagged)]
pub enum ConfigValue {
    Bool(bool),
    Number(f64),
    Str(String),
}

/// Show a config field only while a sibling field holds one of these values — the n8n
/// `displayOptions` idea, as data rather than as an expression language.
#[derive(Clone, Serialize)]
pub struct ShowWhen {
    pub field: String,
    pub equals: Vec<ConfigValue>,
}

impl ShowWhen {
    pub fn new(field: &str, equals: Vec<ConfigValue>) -> Self {
        Self { field: field.to_string(), equals }
    }
}

/// One allowed option for a `Select` config field. Your node is the source of truth for
/// these — the frontend never hardcodes them.
#[derive(Clone, Serialize)]
pub struct SelectOption {
    pub value: String,
    pub label: String,
}

/// One field in your node's **UI / config**. Build the common case with
/// [`ConfigField::new`] and refine with the `with_*` setters.
#[derive(Clone, Serialize)]
pub struct ConfigField {
    pub name: String,
    #[serde(rename = "fieldType")]
    pub field_type: FieldType,
    pub widget: Widget,
    pub required: bool,
    pub description: String,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub options: Vec<SelectOption>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub min: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub max: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub pattern: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none", rename = "maxLength")]
    pub max_length: Option<u32>,
    /// What a NEW instance of your node starts with. AUTHORING-TIME: the editor seeds it,
    /// the engine does not — your `logic` still has to cope with the key being absent.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub default: Option<ConfigValue>,
    /// Show this field only while every condition holds. Empty = always.
    #[serde(skip_serializing_if = "Vec::is_empty", rename = "showWhen")]
    pub show_when: Vec<ShowWhen>,
}

impl ConfigField {
    pub fn new(
        name: &str,
        field_type: FieldType,
        widget: Widget,
        required: bool,
        description: &str,
    ) -> Self {
        Self {
            name: name.to_string(),
            field_type,
            widget,
            required,
            description: description.to_string(),
            options: Vec::new(),
            min: None,
            max: None,
            pattern: None,
            max_length: None,
            default: None,
            show_when: Vec::new(),
        }
    }

    /// The value a new instance starts with.
    pub fn with_default(mut self, default: ConfigValue) -> Self {
        self.default = Some(default);
        self
    }

    /// Show this field only while every condition holds.
    pub fn with_show_when(mut self, conditions: Vec<ShowWhen>) -> Self {
        self.show_when = conditions;
        self
    }

    /// Allowed options for a `Select` widget.
    pub fn with_options(mut self, options: &[(&str, &str)]) -> Self {
        self.options = options
            .iter()
            .map(|(value, label)| SelectOption { value: value.to_string(), label: label.to_string() })
            .collect();
        self
    }

    pub fn with_range(mut self, min: Option<f64>, max: Option<f64>) -> Self {
        self.min = min;
        self.max = max;
        self
    }
}

/// One field of your node's **input or output contract** — the fixed shape of the item
/// data you consume/produce. Declaring these lets the engine detect an edge mismatch and
/// propose an AI field-mapping. Leave a schema empty for a dynamic (any-shape) node.
#[derive(Clone, Serialize)]
pub struct DataField {
    pub name: String,
    #[serde(rename = "fieldType")]
    pub field_type: FieldType,
}

impl DataField {
    pub fn new(name: &str, field_type: FieldType) -> Self {
        Self { name: name.to_string(), field_type }
    }
}

/// Everything the engine needs to catalog and wire your node — declared in code, so the
/// contract and the logic never drift into two files. Serializes to the publish
/// `contract` JSON (plus identity fields the publish request also carries).
#[derive(Serialize)]
pub struct Manifest {
    #[serde(rename = "nodeType")]
    pub node_type: String,
    pub name: String,
    pub version: String,
    /// Presentation metadata: how the node reads in the palette/card. Cosmetic only.
    #[serde(rename = "displayName", skip_serializing_if = "String::is_empty")]
    pub display_name: String,
    #[serde(skip_serializing_if = "String::is_empty")]
    pub description: String,
    /// A curated icon NAME (the frontend maps it to a trusted inline SVG — never raw
    /// markup). Empty -> the frontend's default icon.
    #[serde(skip_serializing_if = "String::is_empty")]
    pub icon: String,
    /// Free-form tags for grouping/search: "tools", "integrations", "science-kit", …
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub tags: Vec<String>,
    #[serde(rename = "configFields")]
    pub config_fields: Vec<ConfigField>,
    #[serde(rename = "inputFields")]
    pub input_fields: Vec<DataField>,
    #[serde(rename = "outputFields")]
    pub output_fields: Vec<DataField>,
}

impl Manifest {
    /// A bare manifest — identity only. Add contract/UI/presentation with the `with_*`
    /// setters. `name` doubles as the display label unless you set [`Manifest::with_display_name`].
    pub fn new(node_type: &str, name: &str, version: &str) -> Self {
        Self {
            node_type: node_type.to_string(),
            name: name.to_string(),
            version: version.to_string(),
            display_name: String::new(),
            description: String::new(),
            icon: String::new(),
            tags: Vec::new(),
            config_fields: Vec::new(),
            input_fields: Vec::new(),
            output_fields: Vec::new(),
        }
    }

    pub fn with_config(mut self, fields: Vec<ConfigField>) -> Self {
        self.config_fields = fields;
        self
    }

    pub fn with_input(mut self, fields: Vec<DataField>) -> Self {
        self.input_fields = fields;
        self
    }

    pub fn with_output(mut self, fields: Vec<DataField>) -> Self {
        self.output_fields = fields;
        self
    }

    /// A palette/card label distinct from `name` (rarely needed; `name` is used otherwise).
    pub fn with_display_name(mut self, display_name: &str) -> Self {
        self.display_name = display_name.to_string();
        self
    }

    /// A one-line description, shown as help and indexed by the palette's fuzzy search.
    pub fn with_description(mut self, description: &str) -> Self {
        self.description = description.to_string();
        self
    }

    /// The curated icon name (see the SDK's icon list). Unknown/empty -> default icon.
    pub fn with_icon(mut self, icon: &str) -> Self {
        self.icon = icon.to_string();
        self
    }

    /// Category tags for grouping and search.
    pub fn with_tags(mut self, tags: &[&str]) -> Self {
        self.tags = tags.iter().map(|t| t.to_string()).collect();
        self
    }
}

// ---------------------------------------------------------------------------------------
// The trait an author implements.
// ---------------------------------------------------------------------------------------

/// Implement this for your node, then hand it to [`export_node!`]. The four methods map
/// one-to-one to the four things a node author declares — and nothing about the ABI,
/// memory, or JSON wire format appears here.
pub trait Node {
    /// Your node's contract + UI (input/output schema, config fields, identity).
    fn manifest(&self) -> Manifest;

    /// Your logic: consume the input batch, return items per output port — or a
    /// [`NodeError`] to fail the node cleanly.
    fn run(&self, input: Input) -> Result<Output, NodeError>;
}

// ---------------------------------------------------------------------------------------
// The ABI glue. `export_node!` wires an author's `Node` to the exports the sandbox calls;
// the functions below do the marshalling. An author never calls these directly.
// ---------------------------------------------------------------------------------------

/// Reserve `len` bytes in the guest and return the pointer. The host writes the input JSON
/// here before calling `run`. Exposed for [`export_node!`]; not for direct use.
///
/// The buffer is deliberately leaked: it lives for the single invocation, and the engine
/// discards the whole WASM store afterward — there is nothing to free into.
#[doc(hidden)]
pub fn __alloc(len: i32) -> i32 {
    let mut buf: Vec<u8> = Vec::with_capacity(len.max(0) as usize);
    let ptr = buf.as_mut_ptr();
    std::mem::forget(buf);
    ptr as i32
}

/// Read the input JSON at `[ptr, ptr+len)`, run `node`, serialize the output JSON, and
/// return an `i64` packing the output pointer (high 32 bits) and length (low 32). Every
/// failure — bad input, a `NodeError`, a serialization slip — becomes the ABI's `error`
/// shape, never a trap. Exposed for [`export_node!`]; not for direct use.
#[doc(hidden)]
pub fn __run(node: &dyn Node, ptr: i32, len: i32) -> i64 {
    let bytes = unsafe { std::slice::from_raw_parts(ptr as *const u8, len.max(0) as usize) };
    let out = run_to_bytes(node, bytes);
    pack(out)
}

/// Return `node`'s manifest as JSON over the ABI (pointer/length packed in an `i64`), for
/// the import tooling to read the contract from the compiled module. Exposed for
/// [`export_node!`].
#[doc(hidden)]
pub fn __describe(node: &dyn Node) -> i64 {
    let bytes = serde_json::to_vec(&node.manifest()).unwrap_or_else(|_| b"{}".to_vec());
    pack(bytes)
}

/// Pure core of `__run`, testable on the host without any WASM: input bytes → output
/// bytes, always well-formed (an internal failure becomes the `error` shape).
fn run_to_bytes(node: &dyn Node, bytes: &[u8]) -> Vec<u8> {
    let parsed: Value = match serde_json::from_slice(bytes) {
        Ok(value) => value,
        Err(e) => return error_bytes("NODE_BAD_INPUT", &format!("input was not valid JSON: {e}")),
    };

    // Unwrap each item's `json` payload; `config` passes through (Null when absent).
    let items = parsed
        .get("items")
        .and_then(Value::as_array)
        .map(|arr| arr.iter().map(|it| it.get("json").cloned().unwrap_or(Value::Null)).collect())
        .unwrap_or_default();
    let config = parsed.get("config").cloned().unwrap_or(Value::Null);

    match node.run(Input { items, config }) {
        Ok(output) => serde_json::to_vec(&output_to_wire(output))
            .unwrap_or_else(|_| error_bytes("NODE_BAD_OUTPUT", "node output was not serializable")),
        Err(err) => error_bytes(&err.code, &err.message),
    }
}

/// Wrap the `Output` ports back into the wire shape the sandbox decodes:
/// `{ "ports": { port: [ { "json": <item> }, … ] } }`.
fn output_to_wire(output: Output) -> Value {
    let ports: serde_json::Map<String, Value> = output
        .ports
        .into_iter()
        .map(|(port, items)| {
            let wrapped: Vec<Value> = items.into_iter().map(|json| json!({ "json": json })).collect();
            (port, Value::Array(wrapped))
        })
        .collect();
    json!({ "ports": ports })
}

fn error_bytes(code: &str, message: &str) -> Vec<u8> {
    serde_json::to_vec(&json!({ "error": { "code": code, "message": message } }))
        .unwrap_or_else(|_| b"{\"error\":{\"code\":\"NODE_INTERNAL\",\"message\":\"\"}}".to_vec())
}

/// Leak `bytes` and pack its pointer (high 32) and length (low 32) into the ABI's `i64`.
fn pack(bytes: Vec<u8>) -> i64 {
    let len = bytes.len() as u64;
    let ptr = bytes.as_ptr() as u64;
    std::mem::forget(bytes);
    ((ptr << 32) | (len & 0xffff_ffff)) as i64
}

/// Generate the WASM exports (`alloc`, `run`, `describe`, `memory`) for your [`Node`].
/// One line, once, at the crate root:
///
/// ```ignore
/// workflow_node::export_node!(MyNode);
/// ```
///
/// `MyNode` must implement [`Node`] and be constructible as `MyNode` (a unit struct) — the
/// macro instantiates it per call. Nothing else is required; the crate must be a `cdylib`
/// (`crate-type = ["cdylib"]`) built for `wasm32-unknown-unknown`.
#[macro_export]
macro_rules! export_node {
    ($node:ty) => {
        #[no_mangle]
        pub extern "C" fn alloc(len: i32) -> i32 {
            $crate::__alloc(len)
        }

        #[no_mangle]
        pub extern "C" fn run(ptr: i32, len: i32) -> i64 {
            $crate::__run(&<$node>::default(), ptr, len)
        }

        #[no_mangle]
        pub extern "C" fn describe() -> i64 {
            $crate::__describe(&<$node>::default())
        }
    };
}

#[cfg(test)]
mod tests {
    use super::*;

    #[derive(Default)]
    struct Doubler;

    impl Node for Doubler {
        fn manifest(&self) -> Manifest {
            Manifest::new("doubler", "Doubler", "1.0.0")
                .with_input(vec![DataField::new("n", FieldType::Number)])
                .with_output(vec![DataField::new("n", FieldType::Number)])
        }

        fn run(&self, input: Input) -> Result<Output, NodeError> {
            let out = input
                .items
                .into_iter()
                .map(|item| {
                    let n = item.get("n").and_then(Value::as_f64).unwrap_or(0.0);
                    json!({ "n": n * 2.0 })
                })
                .collect();
            Ok(Output::items(out))
        }
    }

    #[test]
    fn marshals_input_runs_and_wraps_output() {
        let input = br#"{"items":[{"json":{"n":21}}],"config":null}"#;
        let out = run_to_bytes(&Doubler, input);
        let value: Value = serde_json::from_slice(&out).unwrap();
        assert_eq!(value["ports"]["default"][0]["json"]["n"], json!(42.0));
    }

    #[test]
    fn bad_input_json_becomes_the_error_shape_not_a_panic() {
        let out = run_to_bytes(&Doubler, b"not json");
        let value: Value = serde_json::from_slice(&out).unwrap();
        assert_eq!(value["error"]["code"], json!("NODE_BAD_INPUT"));
    }

    #[test]
    fn a_node_error_surfaces_with_its_code() {
        #[derive(Default)]
        struct Fails;
        impl Node for Fails {
            fn manifest(&self) -> Manifest {
                Manifest::new("fails", "Fails", "1.0.0")
            }
            fn run(&self, _input: Input) -> Result<Output, NodeError> {
                Err(NodeError::new("NOPE", "cannot"))
            }
        }
        let out = run_to_bytes(&Fails, br#"{"items":[],"config":null}"#);
        let value: Value = serde_json::from_slice(&out).unwrap();
        assert_eq!(value["error"]["code"], json!("NOPE"));
        assert_eq!(value["error"]["message"], json!("cannot"));
    }

    #[test]
    fn config_accessors_read_typed_fields() {
        let input = Input {
            items: vec![],
            config: json!({ "text": "hi", "count": 3, "flag": true }),
        };
        assert_eq!(input.config_str("text"), Some("hi"));
        assert_eq!(input.config_f64("count"), Some(3.0));
        assert_eq!(input.config_bool("flag"), Some(true));
        assert_eq!(input.config_str("missing"), None);
    }

    #[test]
    fn manifest_serializes_to_the_contract_shape() {
        let m = Doubler.manifest();
        let value = serde_json::to_value(&m).unwrap();
        assert_eq!(value["nodeType"], json!("doubler"));
        assert_eq!(value["inputFields"][0]["name"], json!("n"));
        assert_eq!(value["inputFields"][0]["fieldType"], json!("number"));
        // A field with no options omits the key rather than emitting [].
        assert!(value.get("configFields").unwrap().as_array().unwrap().is_empty());
    }
}
