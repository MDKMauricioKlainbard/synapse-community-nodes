'use strict';

/**
 * The JavaScript node SDK — the counterpart of `nodes/python/workflow_node.py`.
 *
 * You write four things and nothing else:
 *   1. the input contract, 2. the output contract, 3. the UI/config fields, 4. the logic.
 *
 * Everything else — the wire format, the process boundary, how bytes cross it — is handled
 * here. You never see JSON framing, gRPC, or a socket.
 *
 * ```js
 * const { Node, Manifest, ConfigField, Output, node, STRING, TEXT_FIELD } = require('./workflow-node');
 *
 * class Uppercase extends Node {
 *   manifest() {
 *     return new Manifest({
 *       nodeType: 'uppercase', name: 'Uppercase', version: '1.0.0',
 *       configFields: [new ConfigField('field', STRING, TEXT_FIELD)],
 *     });
 *   }
 *   async logic(inputs, config) {
 *     const field = config.field || 'text';
 *     return Output.items(inputs.map((i) => ({ ...i, [field]: String(i[field]).toUpperCase() })));
 *   }
 * }
 * module.exports = { run: node(new Uppercase()) };
 * ```
 *
 * # Why JavaScript at all
 *
 * Because third-party integrations live here. Nearly every SaaS ships a JS client first, the
 * REST examples in their docs are `fetch` calls, and the webhook payload shapes are written
 * as TypeScript interfaces. A Python or Rust integration node is a translation of something
 * that already exists in JS.
 *
 * # How a node reaches the network — and why it is not `fetch`
 *
 * Through [`http`] below, which does NOT open a connection. It asks the ENGINE to make the
 * request, over a pipe to the worker process. That indirection is the whole point:
 *
 *   * **The secret never reaches your code.** You name a `credentialId`; the engine decrypts
 *     the secret, injects it the way that credential says to (bearer token, custom header,
 *     query parameter), and hands you back only the response. A node cannot leak a secret it
 *     was never given.
 *   * **One outbound door.** SSRF defence, the response size cap and the timeout live in the
 *     engine's gateway, written once. A node calling `fetch` directly would bypass all three.
 *   * **You cannot spend someone else's credential.** The engine resolves the owner from the
 *     running invocation, not from anything this process says.
 *
 * `fetch` is not blocked at the OS level today (see `workers/node/isolation.js`), so a node
 * *can* call it. Doing so is a bug: it loses credential injection and every barrier above.
 */

const { readFileSync } = require('node:fs');

// ---------------------------------------------------------------------------------------
// Field types and widgets — the same vocabulary the Python SDK and the Rust catalog use.
// ---------------------------------------------------------------------------------------

const STRING = 'string';
const NUMBER = 'number';
const BOOL = 'bool';
const OBJECT = 'object';
const LIST = 'list';

const TEXT_FIELD = 'text-field';
const TEXT_AREA = 'text-area';
const NUMBER_FIELD = 'number-field';
const CHECKBOX = 'checkbox';
const SELECT = 'select';
const KEY_VALUE_LIST = 'key-value-list';
const LIST_EDITOR = 'list-editor';
const CREDENTIAL_SELECT = 'credential-select';
const FILE_UPLOAD = 'file-upload';
const SCRIPT = 'script';                 // a Rhai code editor
const PYTHON_SCRIPT = 'python-script';   // a Python code editor, run on a satellite tier (like python_code)
// Widgets that know something the bare field does not (2026-07-28).
const FIELD_SELECTOR = 'field-selector';    // combobox over the fields arriving from upstream
const MULTI_SELECT = 'multi-select';        // several values, stored as a list, not a CSV string
const JSON_EDITOR = 'json-editor';          // object/list edited as JSON, validated live
const REGEX_TESTER = 'regex-tester';        // regex + a sample to try it against
const TIMEZONE_SELECT = 'timezone-select';  // IANA zones, from the browser's own ICU data
const DATE_PICKER = 'date-picker';       // ISO-8601 date/time
const RANGE = 'range';                   // a numeric {start, end} in one row

/** One field the user fills in on your node. This is the UI schema — the frontend renders
 * the form from it, so a node's config is never an opaque blob. */
class ConfigField {
  constructor(name, fieldType, widget, options = {}) {
    this.name = name;
    this.fieldType = fieldType;
    this.widget = widget;
    this.required = options.required ?? false;
    this.description = options.description ?? '';
    /** `[[value, label], …]` — only for SELECT. The node is the source of truth for its
     * own options; the frontend never hardcodes them. */
    this.options = options.options ?? null;
    this.min = options.min ?? null;
    this.max = options.max ?? null;
    this.pattern = options.pattern ?? null;
    this.maxLength = options.maxLength ?? null;
    /** What a NEW instance of the node starts with. AUTHORING-TIME: the editor seeds it,
     * the engine does not — your `logic` still has to handle the key being absent. */
    this.default = options.default ?? null;
    // Starter code a code-editor widget (SCRIPT / PYTHON_SCRIPT) is PRE-LOADED with on a fresh
    // instance — scaffolding the user edits, stating the script's contract. Empty ⇒ none.
    this.templateCode = options.templateCode ?? '';
    /** `[new ShowWhen('mode', ['custom'])]` — show this field only while every condition
     * holds. Empty = always shown. */
    this.showWhen = options.showWhen ?? null;
  }

  toJSON() {
    const out = {
      name: this.name,
      fieldType: this.fieldType,
      widget: this.widget,
      required: this.required,
      description: this.description,
    };
    if (this.options) out.options = this.options.map(([value, label]) => ({ value, label }));
    if (this.min !== null) out.min = this.min;
    if (this.max !== null) out.max = this.max;
    if (this.pattern !== null) out.pattern = this.pattern;
    if (this.maxLength !== null) out.maxLength = this.maxLength;
    // `!== null`, not truthiness: `default: false` and `default: 0` are real defaults, and
    // dropping them is the bug that forced `csv_build` to expose `no_header` instead of
    // `header`.
    if (this.default !== null) out.default = this.default;
    if (this.showWhen) out.showWhen = this.showWhen.map((c) => c.toJSON());
    if (this.templateCode) out.templateCode = this.templateCode;
    return out;
  }
}

/** Show a config field only while a sibling field holds one of these values — the n8n
 * `displayOptions` idea. Values, not expressions: "which mode is selected" is what every
 * real case needs, and a condition language would have to be implemented identically in the
 * engine, three SDKs and the editor. */
class ShowWhen {
  constructor(field, equals) {
    this.field = field;
    this.equals = equals;
  }

  toJSON() {
    return { field: this.field, equals: this.equals };
  }
}

/** One field of your input or output CONTRACT — the shape of the item data, as opposed to
 * the config. Declaring these lets the engine detect an edge mismatch and offer a mapping.
 * Omit them for a dynamic (any-shape) node.
 *
 * Scalars only (`string` | `number` | `bool`): a declared contract is FLAT by design, and
 * the engine refuses a nested one. A sub-object becomes prefixed leaves (`address_city`);
 * repetition is the other axis, because a node receives a BATCH. */
class DataField {
  constructor(name, fieldType) {
    this.name = name;
    this.fieldType = fieldType;
  }
  toJSON() {
    return { name: this.name, fieldType: this.fieldType };
  }
}

/** Everything the engine needs to catalog and wire your node, declared in code so the
 * contract and the logic cannot drift into two files. */
class Manifest {
  constructor({
    nodeType,
    name,
    version,
    configFields = [],
    inputFields = [],
    outputFields = [],
    description = '',
    icon = '',
    tags = [],
    displayName = '',
    tier = '',
    category = '',
    provider = '',
    capabilities = [],
    coordinates = {},
    streaming = false,
    productionModeConfigurable = false,
  }) {
    this.nodeType = nodeType;
    this.name = name;
    this.version = version;
    this.configFields = configFields;
    this.inputFields = inputFields;
    this.outputFields = outputFields;
    this.description = description;
    this.icon = icon;
    this.tags = tags;
    this.displayName = displayName;
    // The curated dependency environment this node needs, `name@version` (e.g.
    // "node-stdlib@1.0.0"). A satellite node declares its ENVIRONMENT by tier name, not by
    // carrying a hand-written lockfile: the build resolves the tier to its exact pinned
    // lockfile (backend/execution-plane/tiers/), so there is one source of truth per
    // environment and an author cannot mint a new pool by choosing their own versions.
    // Empty = the node carries its own lockfile the old way (legacy / pre-tier nodes).
    this.tier = tier;
    // Catalog-organization metadata (node-catalog redesign). `category` is one of the server's
    // controlled slugs — the SERVER is the authority (an unknown value degrades to `other`, and
    // the category is never inferred from tags[0]). `provider` is the product/ecosystem, and
    // `capabilities` are coarse slugs, both for faceting/search. All optional.
    this.category = category;
    this.provider = provider;
    this.capabilities = capabilities;
    // The node's three INTERACTION COORDINATES ("El nodo como flecha"):
    // { origin, destination, cardinality, ports } with the engine's stable slugs. MANDATORY for a
    // real node — an exotic/omitted world trips the catalog's exotic-world alert. Two nodes compose
    // when destination(a) === origin(b).
    this.coordinates = coordinates || {};
    // STREAMING PRODUCTION (Slice 8): true when this node's logic PRODUCES INCREMENTALLY (yields
    // items/chunks instead of returning a full list). The engine dispatches such a node through the
    // streaming path (result-chunk frames, bounded memory, pipeline overlap). Satellite only; the
    // node's actual return type must match this, or the worker fails the mismatch explicitly.
    this.streaming = streaming || false;
    // PRODUCTION MODE (§11.5): when true, the user may retune a streaming node's emission grouping
    // (per-item / chunk / batch) via the reserved `_production` override, like core `csv_stream`.
    this.productionModeConfigurable = productionModeConfigurable || false;
  }

  toJSON() {
    const out = {
      nodeType: this.nodeType,
      name: this.name,
      version: this.version,
      configFields: this.configFields.map((f) => f.toJSON()),
      inputFields: this.inputFields.map((f) => f.toJSON()),
      outputFields: this.outputFields.map((f) => f.toJSON()),
    };
    if (this.displayName) out.displayName = this.displayName;
    if (this.description) out.description = this.description;
    if (this.icon) out.icon = this.icon;
    if (this.tags.length) out.tags = [...this.tags];
    // Catalog-organization metadata (node-catalog redesign) — only emitted when set, so the
    // contract stays minimal. The control-plane validates `category` and derives origin/verified.
    if (this.category) out.category = this.category;
    if (this.provider) out.provider = this.provider;
    if (this.capabilities.length) out.capabilities = [...this.capabilities];
    // The three interaction coordinates ("El nodo como flecha"), copied by pack.py into node.json so
    // the engine ships them to the assistant. Only emitted when declared.
    if (this.coordinates && Object.keys(this.coordinates).length) {
      out.coordinates = { ...this.coordinates };
    }
    // Packaging metadata (like runtime/entrypoint/lockfile), resolved by the packer/engine —
    // not part of the catalog descriptor.
    if (this.tier) out.tier = this.tier;
    // Streaming production (Slice 8): only emitted when true, so a batch node's contract is unchanged.
    if (this.streaming) out.streaming = true;
    // Production-mode opt-in (§11.5): lets the user retune a streaming node's emission grouping.
    if (this.productionModeConfigurable) out.productionModeConfigurable = true;
    return out;
  }
}

/** A clean node failure — your node saying "I cannot process this". Throw it from `logic`.
 *
 * NOTE: the satellite protocol has no per-node error-code channel yet, so the engine reports
 * this as `SATELLITE_NODE_FAILED` with your message. `code` travels inside the message and is
 * forward-looking for when that channel exists. */
class NodeError extends Error {
  constructor(code, message) {
    super(`[${code}] ${message}`);
    this.name = 'NodeError';
    this.code = code;
    this.nodeMessage = message;
  }
}

/** A file attached to an incoming item, already written to your working directory. */
class InputFile {
  constructor({ path, filename, mimeType, sizeBytes }) {
    this.path = path;
    this.filename = filename;
    this.mimeType = mimeType;
    this.sizeBytes = sizeBytes;
  }
  /** The bytes. `path` is a real local path — the worker fetched the content before your
   * node started, because your process deliberately cannot reach the engine. */
  read() {
    return readFileSync(this.path);
  }
}

/** Your node's result: items keyed by output port.
 *
 * Most nodes have one output — `Output.items([...])`. A branching node emits several named
 * ports via `Output.builder().port('true', [...]).port('false', [...]).build()`.
 *
 * A node that PRODUCES a file writes it into the working directory and declares it with
 * `.withFiles([...])`. Declaring is required: the worker uploads exactly what you name, so a
 * scratch file never becomes an artifact by accident. */
class Output {
  static DEFAULT_PORT = 'default';

  constructor(ports, files = []) {
    this._ports = ports;
    this._files = files;
  }

  static items(items) {
    return new Output({ [Output.DEFAULT_PORT]: [...items] });
  }

  static builder() {
    return new OutputBuilder();
  }

  withFiles(paths) {
    this._files = [...paths];
    return this;
  }

  toRaw() {
    const raw = { ports: this._ports };
    if (this._files.length) raw.files = this._files;
    return raw;
  }
}

class OutputBuilder {
  constructor() {
    this._ports = {};
    this._files = [];
  }
  port(name, items) {
    this._ports[name] = [...items];
    return this;
  }
  files(paths) {
    this._files = [...paths];
    return this;
  }
  build() {
    return new Output(this._ports, this._files);
  }
}

/** The base class you extend. Stateless: every per-instance value arrives in `config`. */
class Node {
  /** Return a `Manifest`. Required. */
  manifest() {
    throw new Error('a node must implement manifest()');
  }
  /** Your logic. `inputs` is the batch of item data (plain objects), `config` is what the
   * user filled into your fields, and `files` — only if you declare the parameter — is the
   * `InputFile[]` attached to the batch. Return an `Output` or a plain array of items. */
  async logic() {
    throw new Error('a node must implement logic()');
  }
}

// ---------------------------------------------------------------------------------------
// The capability channel: how a node reaches the engine, over fd 3, via its worker.
// ---------------------------------------------------------------------------------------

/**
 * Capability calls travel over Node's IPC channel to the worker that spawned this process.
 *
 * `process.send` exists ONLY when a parent opened that channel, which makes its presence the
 * exact test for "am I running inside a worker?" — no environment variable to check, no
 * socket path to be handed. A node run outside one (a unit test, the local harness) gets a
 * clear refusal instead of a hang.
 */
let pending = null;

function ensureChannel() {
  if (pending) return pending;
  if (typeof process.send !== 'function') {
    throw new NodeError(
      'CAPABILITY_UNAVAILABLE',
      'this node is not running inside a worker, so it cannot reach the engine',
    );
  }

  const waiters = new Map();
  process.on('message', (message) => {
    if (!message || typeof message.id !== 'number') return;
    const waiter = waiters.get(message.id);
    if (!waiter) return;
    waiters.delete(message.id);
    if (message.ok) waiter.resolve(message.value);
    else waiter.reject(new NodeError('CAPABILITY_FAILED', message.error || 'unknown failure'));
  });
  // If the channel closes, every in-flight call is REJECTED rather than left hanging: a
  // promise that never settles would surface 15 seconds later as a timeout, hiding the cause.
  process.on('disconnect', () => {
    for (const waiter of waiters.values()) {
      waiter.reject(new NodeError('CAPABILITY_FAILED', 'the capability channel closed'));
    }
    waiters.clear();
  });

  pending = { waiters, nextId: 1 };
  return pending;
}

function callCapability(capability, payload) {
  const state = ensureChannel();
  const id = state.nextId++;
  return new Promise((resolve, reject) => {
    state.waiters.set(id, { resolve, reject });
    process.send({ id, capability, payload });
  });
}

/**
 * Make an HTTP request THROUGH THE ENGINE.
 *
 * ```js
 * const res = await http.post('https://discord.com/api/webhooks/…', {
 *   json: { content: 'hello' },
 * });
 * ```
 *
 * `credentialId` is the id of a credential the user picked in the node's config (a
 * `CREDENTIAL_SELECT` field). Pass it and the engine injects the secret; your code never
 * holds it. Without one, the request still goes through the engine's gateway, which refuses
 * private, loopback and metadata addresses.
 *
 * The response is `{ status, body, json() }`. A non-2xx status is NOT thrown — an
 * integration often needs to read a 404 or a 429 and decide. Use `res.ok`.
 */
const http = {
  async request(method, url, options = {}) {
    const headers = { ...(options.headers || {}) };
    let body = options.body;

    if (options.json !== undefined) {
      body = JSON.stringify(options.json);
      // Only if the caller did not set it: some APIs want a vendor content type.
      if (!Object.keys(headers).some((h) => h.toLowerCase() === 'content-type')) {
        headers['Content-Type'] = 'application/json';
      }
    }

    const raw = await callCapability('http', {
      method: String(method).toUpperCase(),
      url,
      headers,
      // Base64 over the channel: the payload is JSON, and a body can be binary.
      body: body === undefined || body === null ? '' : Buffer.from(body).toString('base64'),
      credentialId: options.credentialId || '',
      timeoutMs: options.timeoutMs || 0,
    });

    const responseBody = Buffer.from(raw.body || '', 'base64');
    return {
      status: raw.status,
      ok: raw.status >= 200 && raw.status < 300,
      body: responseBody,
      text: () => responseBody.toString('utf8'),
      json: () => {
        const text = responseBody.toString('utf8');
        try {
          return JSON.parse(text);
        } catch {
          throw new NodeError(
            'HTTP_NOT_JSON',
            `the response was not JSON: ${text.slice(0, 200)}`,
          );
        }
      },
    };
  },
  get(url, options) {
    return http.request('GET', url, options);
  },
  post(url, options) {
    return http.request('POST', url, options);
  },
  put(url, options) {
    return http.request('PUT', url, options);
  },
  patch(url, options) {
    return http.request('PATCH', url, options);
  },
  delete(url, options) {
    return http.request('DELETE', url, options);
  },
};

// ---------------------------------------------------------------------------------------
// The adapter to the raw ABI.
// ---------------------------------------------------------------------------------------

/**
 * Adapts a `Node` to the `run(inputs, config, files)` the satellite worker calls.
 *
 * `files` is passed to `logic` only when it DECLARES the parameter, so a node written
 * without files is called exactly as before. Detected by arity, which is the JS analogue of
 * the Python SDK's signature inspection.
 */
function node(instance) {
  const run = async (inputs, config, files) => {
    const wrapped = (files || []).map((f) => new InputFile(f));
    // `logic.length` counts declared parameters before the first default/rest.
    const result =
      instance.logic.length >= 3
        ? await instance.logic(inputs || [], config || {}, wrapped)
        : await instance.logic(inputs || [], config || {});

    if (result instanceof Output) return result.toRaw();
    if (Array.isArray(result)) return result;
    throw new NodeError('NODE_BAD_OUTPUT', 'logic() must return an Output or an array of items');
  };
  run.manifest = instance.manifest();
  return run;
}

module.exports = {
  STRING, NUMBER, BOOL, OBJECT, LIST,
  TEXT_FIELD, TEXT_AREA, NUMBER_FIELD, CHECKBOX, SELECT,
  KEY_VALUE_LIST, LIST_EDITOR, CREDENTIAL_SELECT, FILE_UPLOAD, SCRIPT, PYTHON_SCRIPT,
  FIELD_SELECTOR, MULTI_SELECT, JSON_EDITOR, REGEX_TESTER, TIMEZONE_SELECT, DATE_PICKER, RANGE,
  ConfigField, DataField, Manifest, NodeError, InputFile, Output, OutputBuilder, Node, ShowWhen,
  node, http,
};
