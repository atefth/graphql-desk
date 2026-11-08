import gql = require("graphql");
import gls = require("graphql-language-service");
import type { CancellationToken, Position, TextDocument, Uri } from "vscode";

type Span = { start: number; end: number };
type Point = { line: number; character: number };
type PositionRange = { start: Point; end: Point };
type Snapshot = { uri: string; version: number; text: string };
type ModuleIdentity = Pick<Snapshot, "uri" | "version"> & { project: string; module: string };
type HostSpan = { module: ModuleIdentity; range: Span };
type EncodedSpan = { codepoint: Span; utf16: Span };
type DocumentIdentity = { module: ModuleIdentity; call_range: EncodedSpan };
type FragmentReference = { target: DocumentIdentity; reference_module: ModuleIdentity; range: EncodedSpan };
type FragmentContext = [project: string, schema: string, token: string];
type RefactorContext = {
  factory: string;
  fragment_insertion: number;
  fragment_mode: "new" | "new_trailing" | "populated" | "empty_list" | "empty_tuple";
  indent: string;
  newline: string;
  scope: string;
  statement_start: number;
};
type Call = {
  identity: DocumentIdentity;
  schema_name?: string | null;
  source: string;
  extract?: RefactorContext | null;
  fragments: readonly FragmentReference[];
  map: { utf16: readonly { decoded: Span; host: HostSpan; linear: boolean }[] };
  index?: {
    schema_name: string;
    context_token: string;
    primary_fragment?: { name: string | null } | null;
    definitions: readonly { kind: string; document: DocumentIdentity; name: string | null }[];
  } | null;
};
type SchemaFact = Pick<Snapshot, "uri" | "version"> & { project: string; schema_name: string; token: string; format: string };
type SchemaSnapshot = SchemaFact & { readonly text: string };
type DiagnosticFact = { module: ModuleIdentity; code: string; range: EncodedSpan; message: string };
type Bundle = {
  request: {
    request_id: string;
    modules: readonly (ModuleIdentity & { source: string })[];
    bindings: readonly { project: string; schemas: readonly string[]; default_schema?: string | null }[];
    schemas: readonly SchemaSnapshot[];
  };
  response: {
    request_id: string;
    schemas: readonly SchemaFact[];
    calls: readonly Call[];
    diagnostics: readonly DiagnosticFact[];
    unused_selections?: readonly { document: DocumentIdentity; diagnostic: DiagnosticFact; range: Span }[];
    persisted?: readonly { module: ModuleIdentity; id_is_literal: boolean; expected_id: string; id: string; id_range: EncodedSpan }[];
    persisted_id_insertions?: readonly { module: ModuleIdentity; range: EncodedSpan; expected_id: string; positional: boolean; insertion: EncodedSpan }[];
  };
};
type Directives = (bundle: Bundle, call: Call) => readonly string[];
type LanguageOptions = {
  analyze: (snapshot: Snapshot, token?: CancellationToken) => Bundle | null | Promise<Bundle | null>;
  isCurrent: (bundle: Bundle, snapshot: Snapshot, token?: CancellationToken) => boolean | Promise<boolean>;
  isTrusted: () => boolean;
  clientDirectives?: Directives;
  mapSnapshot?: (snapshot: Snapshot, bundle: Bundle) => Snapshot;
};
type RegistrationOptions = LanguageOptions & {
  snapshot?: (document: TextDocument) => Snapshot;
  resolveUri?: (uri: string) => Uri;
};
type CallRegistry = Map<string, Call>;
type ASTRegistry = Map<string, gql.DocumentNode | null>;
type FragmentTarget = { node: gql.FragmentDefinitionNode; owner: Call };
type FragmentSymbol = {
  name: string;
  target: FragmentTarget;
  selected: { node: gql.NameNode; owner: Call };
  context: FragmentContext;
};
type FragmentOccurrence = { call: Call; node: gql.NameNode };
type MappedLocation = { module: Pick<ModuleIdentity, "uri" | "version">; range: Span };
type SourceLocation = HostSpan & { sourceText: string; positionRange: PositionRange };
type TextChange = { range: Span; replacement: string };
type SourceEdit = SourceLocation & TextChange;
type QuickFix = TextChange & { title: string; guardOnApply?: boolean };
type DiagnosticInput = { code: unknown; message: string; range: Span };
type FeatureDiagnostic = DiagnosticInput & { code: string; severity: 1 | 2; source: string };
type ValidatedCall = { call: Call; schema: gql.GraphQLSchema; ast: gql.DocumentNode; errors: readonly gql.GraphQLError[]; warnings: readonly gql.GraphQLError[] };
type HoverContent = ReturnType<typeof gls.getHoverInformation>;
type MarkdownInput = Exclude<HoverContent, readonly unknown[]>;
let guardedEditCommandSequence = 0;

const CLIENT_DIRECTIVES = new Set([
  "populate", "client", "unmask", "_unmask", "_optional", "_relayPagination",
  "_simplePagination", "_required", "optional", "required", "arguments",
  "argumentDefinitions", "connection", "refetchable", "relay", "inline",
]);

const key = (identity: unknown) => JSON.stringify(identity);
const documentKey = (identity: DocumentIdentity) => key([identity.module, identity.call_range.codepoint]);
const cancelled = (token?: CancellationToken) => Boolean(token?.isCancellationRequested);

function boundary(text: string, offset: number) {
  return Number.isInteger(offset) && offset >= 0 && offset <= text.length &&
    !(offset > 0 && offset < text.length &&
      /[\uD800-\uDBFF]/.test(text[offset - 1]) && /[\uDC00-\uDFFF]/.test(text[offset]));
}

function positionAt(text: string, offset: number): Point {
  if (!boundary(text, offset)) throw new Error("Invalid character boundary");
  let line = 0;
  let start = 0;
  const newlines = /\r\n|\r|\n/g;
  for (let match; (match = newlines.exec(text)) && match.index < offset;) {
    if (offset < match.index + match[0].length) throw new Error("Split CRLF");
    line += 1;
    start = match.index + match[0].length;
  }
  return { line, character: offset - start };
}

function moduleSource(bundle: Bundle, identity: ModuleIdentity) {
  return bundle.request.modules.find((item) => item.project === identity.project &&
    item.module === identity.module && item.uri === identity.uri && item.version === identity.version);
}

function projectSpan(query: Span, origin: Span, target: Span, linear: boolean): Span[] {
  if (query.start === query.end) {
    if (linear && origin.start <= query.start && query.start <= origin.end) {
      const point = target.start + query.start - origin.start;
      return [{ start: point, end: point }];
    }
    const result = [];
    if (query.start === origin.start) result.push({ start: target.start, end: target.start });
    if (query.start === origin.end) result.push({ start: target.end, end: target.end });
    return result;
  }
  if (origin.start === origin.end || query.start >= origin.end || query.end <= origin.start) return [];
  if (!linear) return [target];
  return [{ start: target.start + Math.max(query.start, origin.start) - origin.start,
    end: target.start + Math.min(query.end, origin.end) - origin.start }];
}

function unique<T>(items: readonly T[]): T[] {
  return [...new Map(items.map((item) => [key(item), item])).values()];
}

function toHost(bundle: Bundle, call: Call, span: Span): HostSpan[] {
  if (!boundary(call.source, span.start) || !boundary(call.source, span.end)) return [];
  return unique(call.map.utf16.flatMap((atom) => {
    const source = moduleSource(bundle, atom.host.module);
    if (!source) return [];
    return projectSpan(span, atom.decoded, atom.host.range, atom.linear)
      .filter((range) => boundary(source.source, range.start) && boundary(source.source, range.end))
      .map((range) => ({ module: atom.host.module, range }));
  }));
}

function toDecoded(call: Call, snapshot: Snapshot, offset: number): Span[] {
  if (!boundary(snapshot.text, offset)) return [];
  return unique(call.map.utf16.filter((atom) => atom.host.module.uri === snapshot.uri &&
    atom.host.module.version === snapshot.version)
    .flatMap((atom) => projectSpan({ start: offset, end: offset }, atom.host.range, atom.decoded, atom.linear))
    .filter((range) => boundary(call.source, range.start) && boundary(call.source, range.end)));
}

function schemaFor(bundle: Bundle, call: Call, schemas?: Map<SchemaSnapshot, gql.GraphQLSchema>): gql.GraphQLSchema | null {
  const project = call.identity.module.project;
  const bindings = bundle.request.bindings.find((item) => item.project === project);
  const name = call.schema_name ?? bindings?.default_schema;
  if (typeof name !== "string" || !bindings?.schemas.includes(name)) return null;
  const snapshot = bundle.request.schemas.find((item) => item.project === project && item.schema_name === name);
  const fact = bundle.response.schemas.find((item) => item.project === project && item.schema_name === name);
  if (!snapshot || !fact || snapshot.uri !== fact.uri || snapshot.version !== fact.version ||
      snapshot.token !== fact.token || snapshot.format !== fact.format) return null;
  const existing = schemas?.get(snapshot);
  if (existing) return existing;
  let schema: gql.GraphQLSchema;
  if (snapshot.format === "sdl") {
    schema = gql.buildSchema(new gql.Source(snapshot.text, snapshot.uri));
  } else {
    const data: unknown = JSON.parse(snapshot.text);
    const introspection = data && typeof data === "object" && "data" in data ? data.data ?? data : data;
    if (!introspection || typeof introspection !== "object" || !("__schema" in introspection)) return null;
    // GraphQL validates the introspection payload while building the schema.
    schema = gql.buildClientSchema(introspection as gql.IntrospectionQuery);
  }
  schemas?.set(snapshot, schema);
  return schema;
}

function fragmentRegistry(bundle: Bundle, call: Call, calls: CallRegistry = new Map(bundle.response.calls.map((item) => [documentKey(item.identity), item])), asts: ASTRegistry = new Map()): gql.FragmentDefinitionNode[] {
  const entries = call.index ? [call.index] : call.fragments
    .map((reference) => calls.get(documentKey(reference.target))?.index).filter(entry => entry != null);
  const result = [];
  const seen = new Set();
  for (const entry of entries) {
    for (const fact of entry.definitions) {
      if (fact.kind !== "fragment" || documentKey(fact.document) === documentKey(call.identity)) continue;
      const owner = calls.get(documentKey(fact.document));
      const ownerKey = documentKey(fact.document);
      if (!owner?.index || owner.index.schema_name !== entry.schema_name ||
          owner.index.context_token !== entry.context_token || seen.has(`${ownerKey}:${fact.name}`)) continue;
      const ast = parseCall(owner, asts);
      if (!ast) continue;
      const definition = ast.definitions.find((node): node is gql.FragmentDefinitionNode => node.kind === gql.Kind.FRAGMENT_DEFINITION && node.name.value === fact.name);
      if (definition) {
        result.push(definition);
        seen.add(`${ownerKey}:${fact.name}`);
      }
    }
  }
  return result;
}

function schemaContext(call: Call): FragmentContext | null | undefined {
  return call.index && [call.identity.module.project, call.index.schema_name, call.index.context_token];
}

function fragmentNodes(bundle: Bundle, call: Call, ast: gql.DocumentNode, calls: CallRegistry = new Map(bundle.response.calls.map((item) => [documentKey(item.identity), item])), asts: ASTRegistry = new Map()): FragmentTarget[] {
  const nodes = [...ast.definitions.filter((item) => item.kind === gql.Kind.FRAGMENT_DEFINITION),
    ...fragmentRegistry(bundle, call, calls, asts)];
  return nodes.flatMap((node) => {
    const owner = node.loc && calls.get(node.loc.source.name);
    return owner ? [{ node, owner }] : [];
  });
}

// Parsed call nodes include locations because parsing never disables them.
function parseCall(call: Call, asts: ASTRegistry = new Map()): gql.DocumentNode | null | undefined {
  const id = documentKey(call.identity);
  if (asts.has(id)) return asts.get(id);
  try {
    const ast = gql.parse(new gql.Source(call.source, id));
    asts.set(id, ast);
    return ast;
  } catch { asts.set(id, null); return null; }
}

function fragmentSymbolAt(bundle: Bundle, call: Call, offset: number, calls?: CallRegistry, asts?: ASTRegistry): FragmentSymbol | null {
  const ast = parseCall(call, asts);
  if (!ast) return null;
  const context = schemaContext(call);
  if (!context) return null;
  const nameNode: { node: gql.FragmentDefinitionNode | gql.FragmentSpreadNode; definition: boolean }[] = [];
  gql.visit(ast, {
    FragmentDefinition(node) { if (node.name.loc!.start <= offset && offset < node.name.loc!.end) nameNode.push({ node, definition: true }); },
    FragmentSpread(node) { if (node.name.loc!.start <= offset && offset < node.name.loc!.end) nameNode.push({ node, definition: false }); },
  });
  if (nameNode.length !== 1) return null;
  const selected = nameNode[0];
  const definitions = fragmentNodes(bundle, call, ast, calls, asts).filter((item) => item.owner.index?.schema_name === context[1] &&
    item.owner.index.context_token === context[2] && item.node.name.value === selected.node.name.value);
  if (selected.definition) {
    const ownerKey = documentKey(call.identity);
    const matches = definitions.filter((item) => documentKey(item.owner.identity) === ownerKey &&
      item.node.name.loc!.start === selected.node.name.loc!.start);
    return matches.length === 1 ? { name: selected.node.name.value, target: matches[0],
      selected: { node: selected.node.name, owner: call }, context } : null;
  }
  return definitions.length === 1 ? { name: selected.node.name.value, target: definitions[0],
    selected: { node: selected.node.name, owner: call }, context } : null;
}

function fragmentOccurrences(bundle: Bundle, symbol: FragmentSymbol, includeDeclaration: boolean, calls: CallRegistry, asts: ASTRegistry): FragmentOccurrence[] {
  const [project, schema, token] = symbol.context;
  const targetKey = documentKey(symbol.target.owner.identity);
  const result = [];
  for (const call of bundle.response.calls) {
    const context = schemaContext(call);
    if (!context || context[0] !== project || context[1] !== schema || context[2] !== token) continue;
    const ast = parseCall(call, asts);
    if (!ast) continue;
    const definitions = fragmentNodes(bundle, call, ast, calls, asts).filter((item) => item.owner.index?.schema_name === schema &&
      item.owner.index.context_token === token && item.node.name.value === symbol.name);
    const definition = definitions.find((item) => documentKey(item.owner.identity) === targetKey &&
      item.node.name.loc!.start === symbol.target.node.name.loc!.start);
    if (includeDeclaration && definition && documentKey(definition.owner.identity) === documentKey(call.identity)) {
      result.push({ call: definition.owner, node: definition.node.name });
    }
    gql.visit(ast, {
      FragmentSpread(node) {
        const matches = definitions.filter((item) => item.node.name.value === node.name.value);
        if (matches.length === 1 && documentKey(matches[0].owner.identity) === targetKey &&
            matches[0].node.name.loc!.start === symbol.target.node.name.loc!.start) {
          result.push({ call, node: node.name });
        }
      },
    });
  }
  return result;
}

function hostFragmentOccurrence(bundle: Bundle, item: FragmentOccurrence): (SourceLocation & { name: string }) | null {
  const snapshot = moduleSource(bundle, item.call.identity.module);
  if (!snapshot) return null;
  const modules = unique(item.call.map.utf16.filter((atom) => atom.decoded.start < item.node.loc!.end &&
    item.node.loc!.start < atom.decoded.end).map((atom) => atom.host.module));
  if (modules.length !== 1 || modules[0].uri !== snapshot.uri || modules[0].version !== snapshot.version) return null;
  const parts = callMappedParts(item.call, item.node.loc!, modules[0]);
  if (!parts) return null;
  const range = { start: parts[0].range.start, end: parts.at(-1)!.range.end };
  if (parts.some((part) => part.linear && item.call.source.slice(part.decoded.start, part.decoded.end) !==
      snapshot.source.slice(part.range.start, part.range.end))) return null;
  return { module: item.call.identity.module, range, name: item.node.value,
    sourceText: snapshot.source,
    positionRange: { start: positionAt(snapshot.source, range.start), end: positionAt(snapshot.source, range.end) } };
}

function callMappedParts(call: Call, span: Span, module: ModuleIdentity) {
  const parts = call.map.utf16.flatMap((atom) => {
    if (key(atom.host.module) !== key(module)) return [];
    if (!atom.linear && (atom.decoded.start < span.start || atom.decoded.end > span.end)) return [];
    const ranges = projectSpan(span, atom.decoded, atom.host.range, atom.linear);
    return ranges.map((range) => ({ decoded: projectSpan(span, atom.decoded,
      { start: atom.decoded.start, end: atom.decoded.end }, true)[0], range, linear: atom.linear }));
  }).filter((part) => part.range.end > part.range.start)
    .sort((a, b) => a.decoded.start - b.decoded.start || a.range.start - b.range.start);
  let decoded = span.start;
  for (let index = 0; index < parts.length; index += 1) {
    const part = parts[index];
    if (part.decoded.start !== decoded || (index && parts[index - 1].range.end !== part.range.start)) return null;
    decoded = part.decoded.end;
  }
  return decoded === span.end ? parts : null;
}

function wordRange(text: string, offset: number): Span {
  let start = offset;
  let end = offset;
  while (start > 0 && /[_0-9A-Za-z]/.test(text[start - 1])) start -= 1;
  while (end < text.length && /[_0-9A-Za-z]/.test(text[end])) end += 1;
  return { start, end };
}

function editableRange(bundle: Bundle, call: Call, span: Span, snapshot: Snapshot): Span | null {
  const origins = toHost(bundle, call, span);
  if (origins.length !== 1) return null;
  const origin = origins[0];
  if (origin.module.uri !== snapshot.uri || origin.module.version !== snapshot.version) return null;
  const matching = call.map.utf16.filter((atom) => atom.linear &&
    atom.decoded.start <= span.start && span.end <= atom.decoded.end &&
    key(atom.host.module) === key(origin.module));
  if (span.start === span.end && matching.length === 0) return origin.range;
  if (matching.length !== 1 || call.source.slice(span.start, span.end) !== snapshot.text.slice(origin.range.start, origin.range.end)) return null;
  return origin.range;
}

function unusedSelectionActions(bundle: Bundle, snapshot: Snapshot, diagnostics: readonly DiagnosticInput[]): QuickFix[] {
  const result = [];
  for (const item of bundle.response.unused_selections ?? []) {
    const diagnostic = item.diagnostic;
    if (diagnostic.module.uri !== snapshot.uri || diagnostic.module.version !== snapshot.version ||
        !diagnostics.some(d => d.code === "graphql-desk.52005" &&
          d.range.start === diagnostic.range.utf16.start && d.range.end === diagnostic.range.utf16.end)) continue;
    const call = bundle.response.calls.find(call => documentKey(call.identity) === documentKey(item.document));
    if (!call || call.identity.module.uri !== snapshot.uri || call.identity.module.version !== snapshot.version) continue;
    const ast = gql.parse(call.source);
    // shortcut: only plain query leaves are removable; expand with fragment/directive safety proofs.
    if (ast.definitions.length !== 1 || ast.definitions[0].kind !== gql.Kind.OPERATION_DEFINITION ||
        ast.definitions[0].operation !== "query" || call.source.includes("#")) continue;
    let unsafe = false;
    const selected: gql.FieldNode[] = [];
    gql.visit(ast, {
      Directive() { unsafe = true; },
      FragmentSpread() { unsafe = true; },
      InlineFragment() { unsafe = true; },
      SelectionSet(node) {
        const field = node.selections.find(field => field.loc!.start === item.range.start && field.loc!.end === item.range.end);
        if (field?.kind === gql.Kind.FIELD && !field.selectionSet && !field.arguments!.length && node.selections.length > 1) selected[0] = field;
      },
    });
    const field = selected[0];
    if (unsafe || !field) continue;
    let { start, end } = field.loc!;
    const lineStart = call.source.lastIndexOf("\n", start - 1) + 1;
    const nextLine = call.source.indexOf("\n", end);
    if (/^[ \t]*$/.test(call.source.slice(lineStart, start)) && nextLine !== -1 &&
        /^[ \t\r]*$/.test(call.source.slice(end, nextLine))) {
      start = lineStart;
      end = nextLine + 1;
    } else {
      const following = /^[ \t,]+/.exec(call.source.slice(end));
      if (following) end += following[0].length;
      else start -= /[ \t,]*$/.exec(call.source.slice(0, start))![0].length;
    }
    const parts = callMappedParts(call, { start, end }, call.identity.module);
    if (!parts || parts.some(part => {
      const decoded = call.source.slice(part.decoded.start, part.decoded.end);
      const host = snapshot.text.slice(part.range.start, part.range.end);
      return part.linear ? decoded !== host : decoded !== "\n" || host !== "\r\n";
    })) continue;
    const range = { start: parts[0].range.start, end: parts.at(-1)!.range.end };
    const callRange = call.identity.call_range.utf16;
    if (range.start < callRange.start || range.end > callRange.end) continue;
    result.push({ title: `Remove unused selection '${field.alias?.value ?? field.name.value}'`, range, replacement: "", guardOnApply: true });
  }
  return unique(result);
}

function nodeLocation(bundle: Bundle, node: gql.ASTNode): MappedLocation[] {
  const location = node?.loc;
  if (!location) return [];
  const owner = bundle.response.calls.find((item) => documentKey(item.identity) === location.source.name);
  if (owner) return toHost(bundle, owner, location);
  const schema = bundle.request.schemas.find((item) => item.uri === location.source.name && item.format === "sdl");
  return schema ? [{ module: { uri: schema.uri, version: schema.version }, range: { start: location.start, end: location.end } }] : [];
}

function highlightNode(node: gql.ASTNode): gql.ASTNode {
  return node.kind !== gql.Kind.VARIABLE && "name" in node && node.name ? node.name :
    "variable" in node ? node.variable ?? node : node;
}

function outlineAst(source: string): gql.DocumentNode | null {
  try { return gql.parse(new gql.Source(source)); }
  catch (error) {
    if (!(error instanceof gql.GraphQLError)) return null;
    const failure = error.positions?.[0] ?? source.length;
    try { return gql.parse(new gql.Source(source.slice(0, failure))); }
    catch {}
    const lexer = new gql.Lexer(new gql.Source(source));
    const starts = [];
    let depth = 0;
    try {
      while (lexer.token.kind !== gql.TokenKind.EOF) {
        const token = lexer.advance();
        if (token.start > failure) break;
        if (depth === 0 && token.kind === gql.TokenKind.NAME &&
          ["query", "mutation", "subscription", "fragment"].includes(token.value)) starts.push(token.start);
        if (token.kind === gql.TokenKind.BRACE_L) depth += 1;
        if (token.kind === gql.TokenKind.BRACE_R) depth -= 1;
      }
    } catch {}
    const incompleteStart = starts.at(-1);
    if (incompleteStart === undefined) return null;
    try { return gql.parse(new gql.Source(source.slice(0, incompleteStart))); }
    catch { return null; }
  }
}

function hoverInformation(schema: gql.GraphQLSchema, text: string, offset: number): HoverContent {
  const position = gls.offsetToPosition(text, offset);
  let contents = gls.getHoverInformation(schema, text, position, undefined, { useMarkdown: true });
  const context = gls.getContextAtPosition(text, position, schema);
  const info = context?.typeInfo;
  if (typeof contents !== "string" || !contents || !info) return contents;
  const kind = context.token.state.kind;
  const parent = gql.isNamedType(info.parentType) ? info.parentType : undefined;
  if ((kind === "Field" || kind === "AliasedField") && info.fieldDef?.args.length) {
    // GLS 5.7 omits field arguments from its hover signature.
    const field = info.fieldDef;
    const name = field.name.startsWith("__") ? field.name : `${parent!.name}.${field.name}`;
    const prefix = `\`\`\`graphql\n${name}: ${field.type}\n\`\`\``;
    let args;
    try {
      args = field.args.map((arg) => {
        const value = arg.defaultValue === undefined ? null : gql.astFromValue(arg.defaultValue, arg.type);
        return `${arg.name}: ${arg.type}${value ? ` = ${gql.print(value)}` : ""}`;
      }).join(", ");
    } catch {
      // shortcut: unprintable custom scalar defaults keep GLS hover until scalar serializers are available.
      return contents;
    }
    if (contents.startsWith(prefix)) contents = `\`\`\`graphql\n${name}(${args}): ${field.type}\n\`\`\`` + contents.slice(prefix.length);
  }
  if (kind === "ObjectField" && info.inputObjectType && info.fieldDef) {
    // GLS 5.7 qualifies input members with the enclosing output type.
    const field = `${info.fieldDef.name}: ${info.type}`;
    const prefix = `\`\`\`graphql\n${parent?.name}.${field}\n\`\`\``;
    if (contents.startsWith(prefix)) contents = `\`\`\`graphql\n${info.inputObjectType.name}.${field}\n\`\`\`` + contents.slice(prefix.length);
  }
  const definition = kind === "Argument" ? info.argDef : kind === "ObjectField" ? info.fieldDef : undefined;
  if (definition && "defaultValue" in definition && definition.defaultValue !== undefined && gql.isInputType(definition.type)) {
    const value = gql.astFromValue(definition.defaultValue, definition.type);
    if (value) contents += `\n\nDefault:\n\`\`\`graphql\n${gql.print(value)}\n\`\`\``;
  }
  return contents;
}

function definitionNodes(schema: gql.GraphQLSchema, ast: gql.DocumentNode, fragments: readonly gql.FragmentDefinitionNode[], offset: number): gql.NameNode[] {
  const result: (gql.NameNode | undefined)[] = [];
  const typeInfo = new gql.TypeInfo(schema);
  let operation: gql.OperationDefinitionNode | undefined;
  function selected(node: gql.ASTNode) {
    return node.loc && node.loc.start <= offset && offset < node.loc.end;
  }
  gql.visit(ast, gql.visitWithTypeInfo(typeInfo, {
    OperationDefinition: { enter(node) { operation = node; }, leave() { operation = undefined; } },
    Field(node) { if (selected(node.name)) result.push(typeInfo.getFieldDef()?.astNode?.name); },
    Argument(node) { if (selected(node.name)) result.push(typeInfo.getArgument()?.astNode?.name); },
    Directive(node) { if (selected(node.name)) result.push(typeInfo.getDirective()?.astNode?.name); },
    NamedType(node) { if (selected(node.name)) result.push(schema.getType(node.name.value)?.astNode?.name); },
    EnumValue(node) { if (selected(node)) result.push(typeInfo.getEnumValue()?.astNode?.name); },
    ObjectField(node) {
      if (!selected(node.name)) return;
      const input = gql.getNamedType(typeInfo.getParentInputType());
      if (gql.isInputObjectType(input)) result.push(input.getFields()[node.name.value]?.astNode?.name);
    },
    FragmentSpread(node) {
      if (!selected(node.name)) return;
      const targets = [...ast.definitions, ...fragments]
        .filter((item): item is gql.FragmentDefinitionNode => item.kind === gql.Kind.FRAGMENT_DEFINITION && item.name.value === node.name.value);
      if (targets.length === 1) result.push(targets[0].name);
    },
    Variable(node) {
      if (selected(node.name)) result.push(operation?.variableDefinitions
        ?.find((item) => item.variable.name.value === node.name.value)?.variable.name);
    },
  }));
  return result.filter(item => item !== undefined);
}

function validationIssues(schema: gql.GraphQLSchema, ast: gql.DocumentNode, fragments: readonly gql.FragmentDefinitionNode[], allowed: ReadonlySet<string>) {
  const document: gql.DocumentNode = { kind: gql.Kind.DOCUMENT, definitions: [...ast.definitions, ...fragments] };
  const errors = gls.validateWithCustomRules(schema, document, [gql.ExecutableDefinitionsRule])
    .filter((error) => !(error.message.startsWith("Unknown directive") &&
      error.nodes?.every((node) => node.kind === gql.Kind.DIRECTIVE && allowed.has(node.name.value) &&
        !schema.getDirective(node.name.value))));
  return { errors, warnings: gql.validate(schema, document, [gql.NoDeprecatedCustomRule]) };
}

function suggestedNames(message: string) {
  const index = message.indexOf("Did you mean ");
  if (index < 0 || /inline fragment/i.test(message.slice(index))) return [];
  return [...message.slice(index).matchAll(/"([_A-Za-z][_0-9A-Za-z]*)"/g)].map((item) => item[1]);
}

function deprecatedReplacement(message: string) {
  const reason = message.split(/is deprecated\.\s*/i)[1] ?? "";
  const match = /\b(?:use|replaced\s+(?:by|with))\s+(?:`([_A-Za-z][_0-9A-Za-z]*)`|"([_A-Za-z][_0-9A-Za-z]*)"|'([_A-Za-z][_0-9A-Za-z]*)'|([_A-Za-z][_0-9A-Za-z]*)(?![_0-9A-Za-z.]))/i.exec(reason);
  return match?.slice(1).find(Boolean) ?? null;
}

function diagnosticMatches(diagnostics: readonly DiagnosticInput[], code: string, range: Span, message: string) {
  return diagnostics.some((item) => item.code === code && item.message === message &&
    item.range.start === range.start && item.range.end === range.end);
}

function suggestionActions(bundle: Bundle, snapshot: Snapshot, diagnostics: readonly DiagnosticInput[], calls: readonly ValidatedCall[]): QuickFix[] {
  const result = [];
  const fixableKinds = new Set([gql.Kind.FIELD, gql.Kind.ARGUMENT, gql.Kind.DIRECTIVE,
    gql.Kind.NAMED_TYPE, gql.Kind.OBJECT_FIELD, gql.Kind.ENUM]);
  for (const call of calls) {
    const parsed = call.ast;
    const nameNodes = (node: gql.ASTNode) => {
      if (!fixableKinds.has(node.kind)) return [];
      if ("name" in node && node.name) return [node.name];
      return node.kind === gql.Kind.ENUM ? [node] : [];
    };
    for (const error of call.errors) {
      const replacements = [...new Set(suggestedNames(error.message))];
      if (!replacements.length) continue;
      for (const node of error.nodes ?? []) for (const name of nameNodes(node)) {
        if (node.loc?.source?.name !== documentKey(call.call.identity)) continue;
        if (!name.loc || typeof name.value !== "string") continue;
        const range = editableRange(bundle, call.call, name.loc, snapshot);
        if (!range || !diagnosticMatches(diagnostics, "graphql.validation", range, error.message)) continue;
        for (const replacement of replacements) {
          if (replacement === name.value) continue;
          result.push({ title: `Change '${name.value}' to '${replacement}'`, range, replacement });
        }
      }
    }
    const typeInfo = new gql.TypeInfo(call.schema);
    const parentTypes = new Map<gql.FieldNode, gql.GraphQLCompositeType | null | undefined>();
    gql.visit(parsed, gql.visitWithTypeInfo(typeInfo, {
      Field(node) { parentTypes.set(node, typeInfo.getParentType()); },
    }));
    for (const warning of call.warnings) for (const node of warning.nodes ?? []) {
      if (node.loc?.source?.name !== documentKey(call.call.identity)) continue;
      if (node.kind !== gql.Kind.FIELD || !node.name?.loc) continue;
      const replacement = deprecatedReplacement(warning.message);
      const parent = parentTypes.get(node);
      if (!replacement || replacement === node.name.value || !parent || !("getFields" in parent)) continue;
      const definition = parent.getFields()[replacement];
      if (!definition || definition.deprecationReason != null) continue;
      const range = editableRange(bundle, call.call, node.name.loc, snapshot);
      if (range && diagnosticMatches(diagnostics, "graphql.deprecation", range, warning.message)) {
        result.push({ title: `Replace deprecated field '${node.name.value}' with '${replacement}'`, range, replacement });
      }
    }
  }
  return unique(result);
}

function extractFragmentPlan(bundle: Bundle, snapshot: Snapshot, selection: Span, directives: Directives): SourceEdit[] | null {
  if (!boundary(snapshot.text, selection.start) || !boundary(snapshot.text, selection.end) ||
      selection.start >= selection.end) return null;
  const trivia = (text: string) => /^(?:[\s,\uFEFF]|#[^\r\n]*(?:\r\n|\r|\n|$))*$/.test(text);
  const plans = [];
  for (const call of bundle.response.calls) {
    const context = call.extract;
    const callRange = call.identity.call_range.utf16;
    if (!context || !call.index || call.identity.module.uri !== snapshot.uri ||
        call.identity.module.version !== snapshot.version || selection.start < callRange.start ||
        selection.end > callRange.end) continue;
    const starts = toDecoded(call, snapshot, selection.start);
    const ends = toDecoded(call, snapshot, selection.end);
    if (starts.length !== 1 || ends.length !== 1) continue;
    const start = starts[0].start, end = ends[0].start;
    if (start >= end || !editableRange(bundle, call, { start, end }, snapshot)) continue;
    const ast = parseCall(call);
    const schema = schemaFor(bundle, call);
    if (!ast || !schema) continue;
    const fragments = fragmentRegistry(bundle, call);
    const allowed = new Set([...CLIENT_DIRECTIVES, ...directives(bundle, call)]);
    if (validationIssues(schema, ast, fragments, allowed).errors.length) continue;
    const info = new gql.TypeInfo(schema);
    const matches: { selected: gql.SelectionNode[]; parent: gql.GraphQLCompositeType; spreads: Set<string> }[] = [];
    gql.visit(ast, gql.visitWithTypeInfo(info, {
      SelectionSet(node) {
        const selected = node.selections.filter(item => start <= item.loc!.start && item.loc!.end <= end);
        const overlapping = node.selections.filter(item => item.loc!.start < end && start < item.loc!.end);
        if (!selected.length || selected.length !== overlapping.length ||
            selected.some(item => item.kind !== gql.Kind.FIELD) ||
            !trivia(call.source.slice(start, selected[0].loc!.start)) ||
            !trivia(call.source.slice(selected.at(-1)!.loc!.end, end))) return;
        const spreads = new Set<string>();
        for (const item of selected) gql.visit(item, { FragmentSpread(spread) { spreads.add(spread.name.value); } });
        const parent = info.getParentType();
        if (gql.isCompositeType(parent)) matches.push({ selected, parent, spreads });
      },
    }));
    if (matches.length !== 1) continue;
    const { selected, parent, spreads } = matches[0];
    const span = { start: selected[0].loc!.start, end: selected.at(-1)!.loc!.end };
    const host = editableRange(bundle, call, span, snapshot);
    if (!host || host.start < callRange.start || host.end > callRange.end) continue;
    const names = new Set();
    for (const candidate of bundle.response.calls) {
      const parsed = parseCall(candidate);
      for (const definition of parsed?.definitions ?? []) {
        if (definition.kind === gql.Kind.FRAGMENT_DEFINITION) names.add(definition.name.value);
      }
      for (const match of candidate.source.matchAll(/\bfragment(?:[\s,]|#[^\r\n]*(?:\r\n|\r|\n|$))+([_A-Za-z][_0-9A-Za-z]*)\b/g)) names.add(match[1]);
    }
    const base = `${parent.name}Fields`;
    let pythonNames = snapshot.text.normalize("NFKC");
    let name = base, binding, suffix = 2;
    do {
      binding = name.replace(/([A-Z]+)([A-Z][a-z])/g, "$1_$2")
        .replace(/([a-z0-9])([A-Z])/g, "$1_$2").toLowerCase();
      if (!names.has(name) && !pythonNames.includes(binding)) break;
      name = `${base}${suffix++}`;
    } while (true);
    const calls = new Map(bundle.response.calls.map(item => [documentKey(item.identity), item]));
    const exposesFragment = (reference: FragmentReference, name: string) => {
      const pending = [calls.get(documentKey(reference.target))];
      const visited = new Set();
      while (pending.length) {
        const target = pending.pop();
        if (!target || visited.has(documentKey(target.identity))) continue;
        if (!target.index?.primary_fragment) return false;
        visited.add(documentKey(target.identity));
        if (target.index?.primary_fragment?.name === name) return true;
        for (const nested of target.fragments ?? []) pending.push(calls.get(documentKey(nested.target)));
      }
      return false;
    };
    const selectedReferences = [];
    let hasInlineDependency = false;
    for (const spread of spreads) {
      const matches = call.fragments.filter(reference => exposesFragment(reference, spread) &&
        reference.reference_module.uri === snapshot.uri && reference.reference_module.version === snapshot.version);
      if (matches.length !== 1) { selectedReferences.length = 0; break; }
      const reference = matches[0];
      const range = reference.range.utf16;
      const target = calls.get(documentKey(reference.target));
      const expression = snapshot.text.slice(range.start, range.end);
      if (!target || range.start < callRange.start || range.end > callRange.end) { selectedReferences.length = 0; break; }
      const targetRange = target.identity.call_range.utf16;
      const inline = target.identity.module.uri === snapshot.uri && target.identity.module.version === snapshot.version &&
        range.start === targetRange.start && range.end === targetRange.end;
      if (!inline && !/^[_A-Za-z][_0-9A-Za-z]*(?:\.[_A-Za-z][_0-9A-Za-z]*)*$/.test(expression)) {
        selectedReferences.length = 0; break;
      }
      hasInlineDependency ||= inline;
      selectedReferences.push({ range, target, expression, inline });
    }
    if (selectedReferences.length !== spreads.size) continue;
    const requiredReferences = [...new Map(selectedReferences.map(item => [`${item.range.start}:${item.range.end}`, item])).values()];
    const references = [];
    const inlineReferenceEdits = [];
    const inlineBindings = new Map();
    if (hasInlineDependency) {
      const dependencies = call.fragments.map(reference => {
        const range = reference.range.utf16;
        const target = calls.get(documentKey(reference.target));
        const expression = snapshot.text.slice(range.start, range.end);
        if (!target || range.start < callRange.start || range.end > callRange.end) return null;
        const targetRange = target.identity.call_range.utf16;
        const inline = target.identity.module.uri === snapshot.uri && target.identity.module.version === snapshot.version &&
          range.start === targetRange.start && range.end === targetRange.end;
        if (!inline && !/^[_A-Za-z][_0-9A-Za-z]*(?:\.[_A-Za-z][_0-9A-Za-z]*)*$/.test(expression)) return null;
        return { range, target, expression };
      }).sort((a, b) => a!.range.start - b!.range.start);
      if (!dependencies.every(item => item !== null)) continue;
      for (const dependency of dependencies) {
        const key = `${dependency.range.start}:${dependency.range.end}`;
        if (inlineBindings.has(key)) continue;
        const fragmentName = dependency.target.index?.primary_fragment?.name ?? "Dependency";
        const baseBinding = `fragment_${fragmentName.replace(/([A-Z]+)([A-Z][a-z])/g, "$1_$2")
          .replace(/([a-z0-9])([A-Z])/g, "$1_$2").toLowerCase()}`;
        let dependencyBinding = baseBinding, dependencySuffix = 2;
        while (pythonNames.includes(dependencyBinding)) dependencyBinding = `${baseBinding}_${dependencySuffix++}`;
        pythonNames += dependencyBinding;
        inlineBindings.set(key, { binding: dependencyBinding, expression: dependency.expression, range: dependency.range });
        inlineReferenceEdits.push({ range: dependency.range, replacement: dependencyBinding });
      }
      for (const selected of requiredReferences) {
        const key = `${selected.range.start}:${selected.range.end}`;
        const dependency = inlineBindings.get(key);
        if (!dependency) { references.length = 0; break; }
        references.push(dependency.binding);
      }
    } else {
      references.push(...selectedReferences.map(item => item.expression));
    }
    if (references.length !== requiredReferences.length) continue;
    const fragment = `fragment ${name} on ${parent.name} {\n${call.source.slice(span.start, span.end)}\n}`;
    const rewritten = `${call.source.slice(0, span.start)}...${name}${call.source.slice(span.end)}\n${fragment}`;
    const rewrittenAst = gql.parse(new gql.Source(rewritten, documentKey(call.identity)));
    if (validationIssues(schema, rewrittenAst, fragments, allowed).errors.length) continue;
    if (!Number.isInteger(context.statement_start) || !Number.isInteger(context.fragment_insertion) ||
        !boundary(snapshot.text, context.statement_start) || !boundary(snapshot.text, context.fragment_insertion) ||
        context.statement_start > callRange.start || context.fragment_insertion < callRange.start ||
        context.fragment_insertion > callRange.end || typeof context.indent !== "string" ||
        !/^[ \t]*$/.test(context.indent) || !["\n", "\r", "\r\n"].includes(context.newline) ||
        !["module", "function"].includes(context.scope) ||
        !/^[_A-Za-z][_0-9A-Za-z]*(?:\.[_A-Za-z][_0-9A-Za-z]*)*$/.test(context.factory) ||
        !/^[_A-Za-z][_0-9A-Za-z]*$/.test(binding)) continue;
    const fragmentCall = `${context.factory}(source=${JSON.stringify(fragment)}, fragments=[${references.join(", ")}])`;
    const compositionValue = context.scope === "function" ? fragmentCall : binding;
    const additions = {
      new: `, fragments=[${compositionValue}]`, new_trailing: ` fragments=[${compositionValue}]`, populated: `${compositionValue}, `,
      empty_list: compositionValue, empty_tuple: `${compositionValue},`,
    };
    const composition = additions[context.fragment_mode];
    if (!composition || (context.fragment_insertion >= host.start && context.fragment_insertion <= host.end)) continue;
    const firstInline = inlineReferenceEdits.find(item => item.range.start === context.fragment_insertion);
    const compositionChange = firstInline
      ? { range: { start: context.fragment_insertion, end: firstInline.range.end },
        replacement: composition + firstInline.replacement }
      : { range: { start: context.fragment_insertion, end: context.fragment_insertion }, replacement: composition };
    const changes = [
      { range: host, replacement: `...${name}` },
      compositionChange,
      ...inlineReferenceEdits.filter(item => item !== firstInline),
    ];
    const prefix = [...inlineBindings.values()].map(item =>
      `${context.indent}${item.binding} = ${item.expression}${context.newline}`,
    ).join("");
    const statement = context.scope === "module" ? `${context.indent}${binding} = ${fragmentCall}${context.newline}` : "";
    if (prefix || statement) changes.push({ range: { start: context.statement_start, end: context.statement_start },
      replacement: prefix + statement });
    plans.push(changes.map(item => ({ ...item, module: call.identity.module, sourceText: snapshot.text,
      positionRange: { start: positionAt(snapshot.text, item.range.start), end: positionAt(snapshot.text, item.range.end) } })));
  }
  return plans.length === 1 ? plans[0] : null;
}

/** All analysis and freshness authority is injected; this module never starts Python. */
function createLanguageFeatures({ analyze, isCurrent, isTrusted, clientDirectives = () => [], mapSnapshot = value => value }: LanguageOptions) {
  if (![analyze, isCurrent, isTrusted].every((item) => typeof item === "function")) throw new Error("Analysis, trust and freshness callbacks are required");
  async function load(snapshot: Snapshot, token?: CancellationToken, { deferCurrent = false } = {}) {
    if (!isTrusted() || cancelled(token)) return null;
    const bundle = await analyze(snapshot, token);
    if (bundle) snapshot = mapSnapshot(snapshot, bundle);
    if (!bundle || cancelled(token) || !isTrusted() || bundle.request.request_id !== bundle.response.request_id ||
      !bundle.request.modules.some((item) => item.uri === snapshot.uri && item.version === snapshot.version && item.source === snapshot.text) ||
      (!deferCurrent && !await isCurrent(bundle, snapshot, token)) || cancelled(token) || !isTrusted()) return null;
    return bundle;
  }
  async function finish<T>(bundle: Bundle, snapshot: Snapshot, token: CancellationToken | undefined, value: T, fallback: T) {
    if (cancelled(token) || !isTrusted() || !await isCurrent(bundle, snapshot, token)) return fallback;
    return !cancelled(token) && isTrusted() ? value : fallback;
  }
  async function at<T>(snapshot: Snapshot, offset: number, token: CancellationToken | undefined, action: (bundle: Bundle, call: Call, schema: gql.GraphQLSchema, decoded: number, snapshot: Snapshot) => T, fallback: NoInfer<T>) {
    try {
      const bundle = await load(snapshot, token);
      if (!bundle) return fallback;
      snapshot = mapSnapshot(snapshot, bundle);
      const candidates = bundle.response.calls.flatMap((call) => toDecoded(call, snapshot, offset).map((point) => ({ call, offset: point.start })));
      if (candidates.length !== 1) return fallback;
      const { call, offset: decoded } = candidates[0];
      const schema = schemaFor(bundle, call);
      if (!schema) return fallback;
      const value = action(bundle, call, schema, decoded, snapshot);
      return await finish(bundle, snapshot, token, value, fallback);
    } catch { return fallback; }
  }
  return {
    async complete(snapshot: Snapshot, offset: number, token?: CancellationToken) {
      return at(snapshot, offset, token, (bundle, call, schema, decoded, snapshot) => {
        const span = wordRange(call.source, decoded);
        const range = editableRange(bundle, call, span, snapshot);
        if (!range) return [];
        const position = gls.offsetToPosition(call.source, decoded);
        const suggestions = gls.getAutocompleteSuggestions(schema, call.source,
          position, undefined, fragmentRegistry(bundle, call));
        if (gls.getContextAtPosition(call.source, position, schema)?.token.state.kind === "Directive") {
          const prefix = call.source.slice(span.start, decoded);
          const existing = new Set(suggestions.map((item) => item.label));
          for (const name of clientDirectives(bundle, call)) {
            if (/^[_A-Za-z][_0-9A-Za-z]*$/.test(name) && name.startsWith(prefix) &&
                !schema.getDirective(name) && !existing.has(name)) {
              suggestions.push({ label: name, detail: "Configured client directive", kind: gls.CompletionItemKind.Function });
              existing.add(name);
            }
          }
        }
        return suggestions
          .filter((item) => /^[$@]?[_A-Za-z][_0-9A-Za-z]*$/.test(item.label))
          .map((item) => ({ label: item.label, detail: item.detail, documentation: item.documentation,
            deprecated: Boolean(item.isDeprecated), kind: item.kind,
            insertText: /[$@]/.test(call.source[span.start - 1] ?? "") ? item.label.replace(/^[$@]/, "") : item.label,
            range }));
      }, []);
    },
    async hover(snapshot: Snapshot, offset: number, token?: CancellationToken) {
      return at(snapshot, offset, token, (bundle, call, schema, decoded, snapshot) => {
        const contents = hoverInformation(schema, call.source, decoded);
        if (!contents || (Array.isArray(contents) && !contents.length)) return null;
        const origins = toHost(bundle, call, wordRange(call.source, decoded))
          .filter((item) => item.module.uri === snapshot.uri && item.module.version === snapshot.version && item.range.start <= offset && offset <= item.range.end);
        return origins.length === 1 ? { contents, range: origins[0].range } : null;
      }, null);
    },
    async definition(snapshot: Snapshot, offset: number, token?: CancellationToken) {
      return at(snapshot, offset, token, (bundle, call, schema, decoded, snapshot) => {
        let ast;
        try { ast = gql.parse(new gql.Source(call.source, documentKey(call.identity))); }
        catch { return []; }
        return unique(definitionNodes(schema, ast, fragmentRegistry(bundle, call), decoded)
          .flatMap((node) => nodeLocation(bundle, node))).flatMap((item) => {
            const text = bundle.request.modules.find((source) => source.uri === item.module.uri && source.version === item.module.version)?.source ??
              bundle.request.schemas.find((source) => source.uri === item.module.uri && source.version === item.module.version)?.text;
            return text === undefined ? [] : [{ ...item, positionRange: {
              start: positionAt(text, item.range.start), end: positionAt(text, item.range.end),
            } }];
          });
      }, []);
    },
    async references(snapshot: Snapshot, offset: number, includeDeclaration: boolean, token?: CancellationToken) {
      return at(snapshot, offset, token, (bundle, call, _schema, decoded) => {
        const calls = new Map(bundle.response.calls.map((item) => [documentKey(item.identity), item]));
        const asts = new Map();
        const symbol = fragmentSymbolAt(bundle, call, decoded, calls, asts);
        if (!symbol) return [];
        return unique(fragmentOccurrences(bundle, symbol, includeDeclaration, calls, asts)
          .map((item) => hostFragmentOccurrence(bundle, item)).filter(item => item !== null));
      }, []);
    },
    async prepareRename(snapshot: Snapshot, offset: number, token?: CancellationToken) {
      return at(snapshot, offset, token, (bundle, call, _schema, decoded) => {
        const symbol = fragmentSymbolAt(bundle, call, decoded);
        if (!symbol) return null;
        const item = hostFragmentOccurrence(bundle, {
          call: symbol.selected.owner, node: symbol.selected.node,
        });
        return item && item.module.uri === snapshot.uri && item.module.version === snapshot.version
          ? { range: item.range, positionRange: item.positionRange, placeholder: item.name } : null;
      }, null);
    },
    async rename(snapshot: Snapshot, offset: number, newName: string, token?: CancellationToken) {
      if (!/^[_A-Za-z][_0-9A-Za-z]*$/.test(newName)) return null;
      return at(snapshot, offset, token, (bundle, call, _schema, decoded) => {
        const calls = new Map(bundle.response.calls.map((item) => [documentKey(item.identity), item]));
        const asts = new Map();
        const symbol = fragmentSymbolAt(bundle, call, decoded, calls, asts);
        if (!symbol) return null;
        const [project, schema, contextToken] = symbol.context;
        const targetKey = documentKey(symbol.target.owner.identity);
        const targetPosition = symbol.target.node.name.loc!.start;
        for (const candidate of bundle.response.calls) {
          const context = schemaContext(candidate);
          if (!context || context[0] !== project || context[1] !== schema || context[2] !== contextToken) continue;
          const ast = parseCall(candidate, asts);
          if (!ast) continue;
          const definitions = fragmentNodes(bundle, candidate, ast, calls, asts);
          const targetPresent = definitions.some((item) => documentKey(item.owner.identity) === targetKey &&
            item.node.name.loc!.start === targetPosition);
          const collision = definitions.some((item) => item.node.name.value === newName &&
            (documentKey(item.owner.identity) !== targetKey || item.node.name.loc!.start !== targetPosition));
          if (targetPresent && collision) return null;
        }
        const occurrences = fragmentOccurrences(bundle, symbol, true, calls, asts);
        const edits = occurrences.map((item) => hostFragmentOccurrence(bundle, item));
        if (!edits.length || !edits.every(item => item !== null)) return null;
        const uniqueEdits = unique(edits);
        return uniqueEdits.length === edits.length
          ? uniqueEdits.map(({ module, range, positionRange, sourceText }) => ({ module, range, positionRange, sourceText, replacement: newName })) : null;
      }, null);
    },
    async extractFragment(snapshot: Snapshot, selection: Span, token?: CancellationToken) {
      try {
        const bundle = await load(snapshot, token);
        if (!bundle) return null;
        snapshot = mapSnapshot(snapshot, bundle);
        const plan = extractFragmentPlan(bundle, snapshot, selection, clientDirectives);
        return await finish(bundle, snapshot, token, plan, null);
      } catch { return null; }
    },
    async symbols(snapshot: Snapshot, token?: CancellationToken) {
      try {
        const bundle = await load(snapshot, token);
        if (!bundle) return [];
        snapshot = mapSnapshot(snapshot, bundle);
        const result = [];
        const seen = new Set();
        for (const call of bundle.response.calls) {
          const owner = moduleSource(bundle, call.identity.module);
          if (!owner || owner.uri !== snapshot.uri || owner.version !== snapshot.version) continue;
          const ast = outlineAst(call.source);
          if (!ast) continue;
          const callRange = call.identity.call_range.utf16;
          for (const node of ast.definitions) {
            if ((node.kind !== gql.Kind.OPERATION_DEFINITION && node.kind !== gql.Kind.FRAGMENT_DEFINITION) || !node.name?.loc) continue;
            const origins = toHost(bundle, call, node.loc!);
            const names = toHost(bundle, call, node.name.loc);
            if (names.length !== 1 || names[0].module.uri !== snapshot.uri ||
              names[0].module.version !== snapshot.version || origins.some((item) =>
                item.module.uri !== snapshot.uri || item.module.version !== snapshot.version ||
                item.range.start < callRange.start || item.range.end > callRange.end)) continue;
            const definitionRange = origins.length === 1 ? origins[0].range : callRange;
            const selectionRange = names[0].range;
            if (selectionRange.start < definitionRange.start || selectionRange.end > definitionRange.end) continue;
            const kind = node.kind === gql.Kind.FRAGMENT_DEFINITION ? "fragment" : "operation";
            const symbolKey = key([node.name.value, kind, names[0].module.uri, names[0].module.version, selectionRange]);
            if (seen.has(symbolKey)) continue;
            seen.add(symbolKey);
            result.push({
              name: node.name.value,
              detail: node.kind === gql.Kind.FRAGMENT_DEFINITION ? `fragment on ${node.typeCondition.name.value}` : node.operation,
              kind,
              range: definitionRange,
              selectionRange,
            });
          }
        }
        return await finish(bundle, snapshot, token, unique(result), []);
      } catch { return []; }
    },
    async codeActions(snapshot: Snapshot, diagnostics: readonly DiagnosticInput[], token?: CancellationToken): Promise<QuickFix[]> {
      try {
        const bundle = await load(snapshot, token);
        if (!bundle) return [];
        snapshot = mapSnapshot(snapshot, bundle);
        const staleRanges = diagnostics
          .filter((item) => item.code === "graphql-desk.persisted-hash-mismatch")
          .map((item) => item.range);
        const missingRanges = diagnostics
          .filter((item) => item.code === "graphql-desk.persisted-id-missing")
          .map((item) => item.range);
        const stale = (bundle.response.persisted ?? [])
          .filter((item) => item.module.uri === snapshot.uri &&
            item.module.version === snapshot.version && item.id_is_literal &&
            item.expected_id !== item.id &&
            staleRanges.some((range) => range.start === item.id_range.utf16.start &&
              range.end === item.id_range.utf16.end))
          .map((item) => ({
            title: "Update persisted ID to current document hash",
            range: item.id_range.utf16,
            replacement: JSON.stringify(item.expected_id),
          }));
        const insertions = (bundle.response.persisted_id_insertions ?? [])
          .filter((item) => item.module.uri === snapshot.uri &&
            item.module.version === snapshot.version &&
            missingRanges.some((range) => range.start === item.range.utf16.start &&
              range.end === item.range.utf16.end))
          .map((item) => ({
            title: "Insert persisted ID for current document hash",
            range: item.insertion.utf16,
            replacement: item.positional ? `${JSON.stringify(item.expected_id)}, ` : `id=${JSON.stringify(item.expected_id)}, `,
          }));
        let semantic: QuickFix[] = [];
        if (diagnostics.some(item => item.code === "graphql.validation" || item.code === "graphql.deprecation")) {
          const validated = [];
          const schemas = new Map<SchemaSnapshot, gql.GraphQLSchema>();
          const calls: CallRegistry = new Map(bundle.response.calls.map(item => [documentKey(item.identity), item]));
          const asts: ASTRegistry = new Map();
          for (const call of bundle.response.calls) {
            const schema = schemaFor(bundle, call, schemas);
            if (!schema) continue;
            let ast;
            try { ast = gql.parse(new gql.Source(call.source, documentKey(call.identity))); }
            catch (error) { if (error instanceof gql.GraphQLError) continue; throw error; }
            const fragments = fragmentRegistry(bundle, call, calls, asts);
            const issues = validationIssues(schema, ast, fragments,
              new Set([...CLIENT_DIRECTIVES, ...clientDirectives(bundle, call)]));
            validated.push({ call, schema, ast: { ...ast, definitions: [...ast.definitions, ...fragments] }, ...issues });
          }
          semantic = suggestionActions(bundle, snapshot, diagnostics, validated);
        }
        const removals = unusedSelectionActions(bundle, snapshot, diagnostics);
        return await finish(bundle, snapshot, token, [...stale, ...insertions, ...semantic, ...removals], []);
      } catch { return []; }
    },
    async diagnostics(snapshot: Snapshot, token?: CancellationToken) {
      const bundle = await load(snapshot, token, { deferCurrent: true }).catch(() => null);
      if (!bundle) return [];
      let diagnostics: FeatureDiagnostic[] = [];
      try {
        snapshot = mapSnapshot(snapshot, bundle);
        const result: FeatureDiagnostic[] = bundle.response.diagnostics
          .filter((item) => item.module.uri === snapshot.uri && item.module.version === snapshot.version && item.code !== "invalid-fragments")
          .map((item) => ({ range: item.range.utf16, message: item.message,
            severity: ["unused-colocated-fragment", "52005"].includes(item.code) ? 2 : 1,
            code: `graphql-desk.${item.code}`, source: "GraphQL Desk" }));
        const schemas = new Map<SchemaSnapshot, gql.GraphQLSchema>();
        const calls: CallRegistry = new Map(bundle.response.calls.map(item => [documentKey(item.identity), item]));
        const asts: ASTRegistry = new Map();
        for (const call of bundle.response.calls) {
          const schema = schemaFor(bundle, call, schemas);
          if (!schema) continue;
          const allowed = new Set([...CLIENT_DIRECTIVES, ...clientDirectives(bundle, call)]);
          let ast;
          try { ast = gql.parse(new gql.Source(call.source, documentKey(call.identity))); }
          catch (error) {
            if (!(error instanceof gql.GraphQLError)) throw error;
            for (const position of error.positions ?? []) {
              const end = position < call.source.length
                ? position + (boundary(call.source, position + 1) ? 1 : 2)
                : position;
              for (const origin of toHost(bundle, call, { start: position, end })) {
                if (origin.module.uri === snapshot.uri && origin.module.version === snapshot.version) result.push({
                  range: origin.range, message: error.message, severity: 1, code: "graphql.syntax", source: "GraphQL Desk",
                });
              }
            }
            continue;
          }
          const { errors, warnings } = validationIssues(schema, ast, fragmentRegistry(bundle, call, calls, asts), allowed);
          for (const [issues, severity, code] of [[errors, 1, "graphql.validation"], [warnings, 2, "graphql.deprecation"]] as const) {
            for (const error of issues) {
              for (const node of error.nodes ?? []) {
                for (const origin of nodeLocation(bundle, highlightNode(node))) {
                  if (origin.module.uri === snapshot.uri && origin.module.version === snapshot.version) result.push({
                    range: origin.range, message: error.message, severity, code, source: "GraphQL Desk",
                  });
                }
              }
            }
          }
        }
        for (const item of bundle.response.diagnostics) {
          if (item.code !== "invalid-fragments" || item.module.uri !== snapshot.uri || item.module.version !== snapshot.version) continue;
          if (!result.some((native) => native.source === "GraphQL Desk" &&
            (native.message === item.message || item.message.startsWith(`${native.message}\n`)))) {
            result.push({ range: item.range.utf16, message: item.message, severity: 1,
              code: "graphql-desk.invalid-fragments", source: "GraphQL Desk" });
          }
        }
        diagnostics = unique(result);
      } catch { /* A failed calculation still needs to discard stale captures. */ }
      return finish(bundle, snapshot, token, diagnostics, []).catch(() => []);
    },
  };
}

function registerLanguageFeatures(vscode: typeof import("vscode"), options: RegistrationOptions) {
  let disposed = false;
  const generations = new Map<string, number>();
  const service = createLanguageFeatures({ ...options,
    isCurrent: async (...args) => !disposed && await options.isCurrent(...args) && !disposed,
  });
  const guardedEditCommand = `graphqlDesk.applyGuardedEdit.${++guardedEditCommandSequence}`;
  const selector = [{ language: "python", scheme: "file" }, { language: "python", scheme: "untitled" }];
  const snapshot = options.snapshot ?? ((document: TextDocument) => ({ uri: document.uri.toString(), version: document.version, text: document.getText() }));
  const range = (document: TextDocument, span: Span) => new vscode.Range(document.positionAt(span.start), document.positionAt(span.end));
  const collection = vscode.languages.createDiagnosticCollection("graphql-desk");
  const markdown = (value: MarkdownInput) => {
    const content = new vscode.MarkdownString(typeof value === "string" ? value : value.value ?? "");
    content.isTrusted = false;
    content.supportHtml = false;
    return content;
  };
  const handles = [collection,
    vscode.commands.registerCommand(guardedEditCommand, async (params: unknown) => {
      if (disposed || !params || typeof params !== "object") return false;
      const { uri, version, range: span, expected, replacement } = params as Record<string, unknown>;
      if (typeof uri !== "string" || typeof version !== "number" || !Number.isInteger(version) ||
          !span || typeof span !== "object" || !("start" in span) || !("end" in span) ||
          typeof span.start !== "number" || !Number.isInteger(span.start) ||
          typeof span.end !== "number" || !Number.isInteger(span.end) ||
          typeof expected !== "string" || typeof replacement !== "string") return false;
      const { start, end } = span;
      const document = vscode.workspace.textDocuments.find(item => item.uri.toString() === uri);
      if (!document || document.isClosed || document.version !== version) return false;
      const text = document.getText();
      if (start < 0 || end < start || end > text.length || text.slice(start, end) !== expected) return false;
      const editor = vscode.window.visibleTextEditors.find(item => item.document === document);
      if (!editor) return false;
      let accepted = false;
      const applied = await editor.edit(edit => {
        if (disposed || document.isClosed || document.version !== version ||
            document.getText().slice(start, end) !== expected) return;
        edit.replace(range(document, { start, end }), replacement);
        accepted = true;
      });
      return accepted && applied;
    }),
    vscode.languages.registerCompletionItemProvider(selector, {
      async provideCompletionItems(document, position, token) {
        return (await service.complete(snapshot(document), document.offsetAt(position), token)).map((item) => {
          const completion = new vscode.CompletionItem(item.label, item.kind ? item.kind - 1 : undefined);
          completion.detail = item.detail;
          completion.documentation = item.documentation ? markdown(item.documentation) : undefined;
          completion.insertText = item.insertText;
          completion.range = range(document, item.range);
          if (item.deprecated) completion.tags = [vscode.CompletionItemTag.Deprecated];
          return completion;
        });
      },
    }, " ", ".", "@", "$"),
    vscode.languages.registerHoverProvider(selector, {
      async provideHover(document, position, token) {
        const item = await service.hover(snapshot(document), document.offsetAt(position), token);
        if (!item) return undefined;
        return new vscode.Hover((Array.isArray(item.contents) ? item.contents : [item.contents]).map(markdown), range(document, item.range));
      },
    }),
    // VS Code accepts plain line/character pairs for Range endpoints at runtime.
    vscode.languages.registerDefinitionProvider(selector, {
      async provideDefinition(document, position, token) {
        const items = await service.definition(snapshot(document), document.offsetAt(position), token);
        return items.map((item) => new vscode.Location(options.resolveUri ? options.resolveUri(item.module.uri) : vscode.Uri.parse(item.module.uri),
          new vscode.Range(item.positionRange.start as Position, item.positionRange.end as Position)));
      },
    }),
    vscode.languages.registerReferenceProvider(selector, {
      async provideReferences(document, position, context, token) {
        const items = await service.references(snapshot(document), document.offsetAt(position),
          Boolean(context.includeDeclaration), token);
        return items.map((item) => new vscode.Location(options.resolveUri ? options.resolveUri(item.module.uri) : vscode.Uri.parse(item.module.uri),
          new vscode.Range(item.positionRange.start as Position, item.positionRange.end as Position)));
      },
    }),
    vscode.languages.registerDocumentSymbolProvider(selector, {
      async provideDocumentSymbols(document, token) {
        return (await service.symbols(snapshot(document), token)).map(item => new vscode.DocumentSymbol(
          item.name, item.detail,
          item.kind === "fragment" ? vscode.SymbolKind.Struct : vscode.SymbolKind.Function,
          range(document, item.range), range(document, item.selectionRange)));
      },
    }),
    vscode.languages.registerCodeActionsProvider(selector, {
      async provideCodeActions(document, selectedRange, context, token) {
        const version = document.version;
        const selection = selectedRange && { start: document.offsetAt(selectedRange.start), end: document.offsetAt(selectedRange.end) };
        const current = () => !cancelled(token) && document.version === version && !document.isClosed;
        const diagnostics = context.diagnostics.filter((item) =>
          typeof item.code === "string" && ["graphql-desk.persisted-hash-mismatch", "graphql-desk.persisted-id-missing",
            "graphql.validation", "graphql.deprecation", "graphql-desk.52005"].includes(item.code));
        const items = diagnostics.map((item) => ({
          code: item.code,
          message: item.message,
          range: {
            start: document.offsetAt(item.range.start),
            end: document.offsetAt(item.range.end),
          },
        }));
        const quickFixes = items.length && (!context.only || context.only.contains(vscode.CodeActionKind.QuickFix))
          ? await service.codeActions(snapshot(document), items, token) : [];
        const actions = quickFixes.map((item) => {
          const action = new vscode.CodeAction(item.title, vscode.CodeActionKind.QuickFix);
          if (item.guardOnApply) {
            action.command = { command: guardedEditCommand, title: item.title,
              arguments: [{ uri: document.uri.toString(), version, range: item.range,
                expected: document.getText().slice(item.range.start, item.range.end), replacement: item.replacement }] };
          } else {
            action.edit = new vscode.WorkspaceEdit();
            action.edit.replace(document.uri, range(document, item.range), item.replacement);
          }
          action.diagnostics = diagnostics;
          return action;
        });
        if (!current()) return [];
        if (document.uri.scheme === "file" && (!context.only || context.only.contains(vscode.CodeActionKind.RefactorExtract)) && selection &&
            selection.start < selection.end && await service.extractFragment(snapshot(document), selection, token) && current()) {
          const action = new vscode.CodeAction("Extract GraphQL fragment", vscode.CodeActionKind.RefactorExtract);
          action.command = { command: "graphqlDesk.extractFragment", title: action.title,
            arguments: [{ textDocument: { uri: document.uri.toString() }, version, selection }] };
          actions.push(action);
        }
        return current() ? actions : [];
      },
    }),
  ];
  return {
    async prepareRename(document: TextDocument, position: Position, token?: CancellationToken) {
      if (disposed || cancelled(token)) return null;
      return service.prepareRename(snapshot(document), document.offsetAt(position), token);
    },
    async rename(document: TextDocument, position: Position, newName: string, token?: CancellationToken) {
      if (disposed || cancelled(token)) return null;
      return service.rename(snapshot(document), document.offsetAt(position), newName, token);
    },
    async extractFragment(document: TextDocument, selection: Span, token?: CancellationToken) {
      if (disposed || cancelled(token)) return null;
      const source = snapshot(document);
      const plan = await service.extractFragment(source, selection, token);
      // Extraction only edits its originating file; use the editor's actual URI
      // for LSP synchronization even if analysis canonicalizes filesystem case.
      if (!plan || plan.some(item => item.sourceText !== source.text || item.module.version !== source.version ||
          item.module.uri !== plan[0].module.uri)) return null;
      return plan.map(item => ({ ...item, module: { ...item.module, uri: document.uri.toString() } }));
    },
    async refresh(document: TextDocument, token?: CancellationToken) {
      if (disposed) return;
      const uri = document.uri.toString();
      const generation = (generations.get(uri) ?? 0) + 1;
      generations.set(uri, generation);
      const version = document.version;
      const diagnostics = await service.diagnostics(snapshot(document), token);
      if (disposed || generations.get(uri) !== generation || document.version !== version || cancelled(token)) return;
      collection.set(document.uri, diagnostics.map((item) => {
        const diagnostic = new vscode.Diagnostic(range(document, item.range), item.message, item.severity - 1);
        diagnostic.source = item.source;
        diagnostic.code = item.code;
        return diagnostic;
      }));
    },
    close(uri: Uri) { generations.set(uri.toString(), (generations.get(uri.toString()) ?? 0) + 1); collection.delete(uri); },
    dispose() { disposed = true; generations.clear(); handles.forEach((handle) => handle.dispose()); },
  };
}

export = { createLanguageFeatures, registerLanguageFeatures, toHost, toDecoded, positionAt, boundary, documentKey,
  };
