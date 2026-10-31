"use strict";

const gql = require("graphql");
const gls = require("graphql-language-service");

const CLIENT_DIRECTIVES = new Set([
  "populate", "client", "unmask", "_unmask", "_optional", "_relayPagination",
  "_simplePagination", "_required", "optional", "required", "arguments",
  "argumentDefinitions", "connection", "refetchable", "relay", "inline",
]);

const key = (identity) => JSON.stringify(identity);
const documentKey = (identity) => key([identity.module, identity.call_range.codepoint]);
const cancelled = (token) => Boolean(token?.isCancellationRequested);

function boundary(text, offset) {
  return Number.isInteger(offset) && offset >= 0 && offset <= text.length &&
    !(offset > 0 && offset < text.length &&
      /[\uD800-\uDBFF]/.test(text[offset - 1]) && /[\uDC00-\uDFFF]/.test(text[offset]));
}

function positionAt(text, offset) {
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

function moduleSource(bundle, identity) {
  return bundle.request.modules.find((item) => item.project === identity.project &&
    item.module === identity.module && item.uri === identity.uri && item.version === identity.version);
}

function projectSpan(query, origin, target, linear) {
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

function unique(items) {
  return [...new Map(items.map((item) => [key(item), item])).values()];
}

function toHost(bundle, call, span) {
  if (!boundary(call.source, span.start) || !boundary(call.source, span.end)) return [];
  return unique(call.map.utf16.flatMap((atom) => {
    const source = moduleSource(bundle, atom.host.module);
    if (!source) return [];
    return projectSpan(span, atom.decoded, atom.host.range, atom.linear)
      .filter((range) => boundary(source.source, range.start) && boundary(source.source, range.end))
      .map((range) => ({ module: atom.host.module, range }));
  }));
}

function toDecoded(call, snapshot, offset) {
  if (!boundary(snapshot.text, offset)) return [];
  return unique(call.map.utf16.filter((atom) => atom.host.module.uri === snapshot.uri &&
    atom.host.module.version === snapshot.version)
    .flatMap((atom) => projectSpan({ start: offset, end: offset }, atom.host.range, atom.decoded, atom.linear))
    .filter((range) => boundary(call.source, range.start) && boundary(call.source, range.end)));
}

function schemaFor(bundle, call) {
  const project = call.identity.module.project;
  const bindings = bundle.request.bindings.find((item) => item.project === project);
  const name = call.schema_name ?? bindings?.default_schema;
  if (!bindings?.schemas.includes(name)) return null;
  const snapshot = bundle.request.schemas.find((item) => item.project === project && item.schema_name === name);
  const fact = bundle.response.schemas.find((item) => item.project === project && item.schema_name === name);
  if (!snapshot || !fact || snapshot.uri !== fact.uri || snapshot.version !== fact.version ||
      snapshot.token !== fact.token || snapshot.format !== fact.format) return null;
  if (snapshot.format === "sdl") return gql.buildSchema(new gql.Source(snapshot.text, snapshot.uri));
  const data = JSON.parse(snapshot.text);
  return gql.buildClientSchema(data.data ?? data);
}

function fragmentRegistry(bundle, call, calls = new Map(bundle.response.calls.map((item) => [documentKey(item.identity), item])), asts = new Map()) {
  const entries = call.index ? [call.index] : call.fragments
    .map((reference) => calls.get(documentKey(reference.target))?.index).filter(Boolean);
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
      const definition = ast.definitions.find((node) => node.kind === gql.Kind.FRAGMENT_DEFINITION && node.name.value === fact.name);
      if (definition) {
        result.push(definition);
        seen.add(`${ownerKey}:${fact.name}`);
      }
    }
  }
  return result;
}

function schemaContext(call) {
  return call.index && [call.identity.module.project, call.index.schema_name, call.index.context_token];
}

function fragmentNodes(bundle, call, ast, calls = new Map(bundle.response.calls.map((item) => [documentKey(item.identity), item])), asts = new Map()) {
  const nodes = [...ast.definitions.filter((item) => item.kind === gql.Kind.FRAGMENT_DEFINITION),
    ...fragmentRegistry(bundle, call, calls, asts)];
  return nodes.flatMap((node) => {
    const owner = calls.get(node.loc?.source?.name);
    return owner ? [{ node, owner }] : [];
  });
}

function parseCall(call, asts = new Map()) {
  const id = documentKey(call.identity);
  if (asts.has(id)) return asts.get(id);
  try {
    const ast = gql.parse(new gql.Source(call.source, id));
    asts.set(id, ast);
    return ast;
  } catch { asts.set(id, null); return null; }
}

function fragmentSymbolAt(bundle, call, offset, calls, asts) {
  const ast = parseCall(call, asts);
  if (!ast) return null;
  const context = schemaContext(call);
  if (!context) return null;
  const nameNode = [];
  gql.visit(ast, {
    FragmentDefinition(node) { if (node.name.loc.start <= offset && offset < node.name.loc.end) nameNode.push({ node, definition: true }); },
    FragmentSpread(node) { if (node.name.loc.start <= offset && offset < node.name.loc.end) nameNode.push({ node, definition: false }); },
  });
  if (nameNode.length !== 1) return null;
  const selected = nameNode[0];
  const definitions = fragmentNodes(bundle, call, ast, calls, asts).filter((item) => item.owner.index?.schema_name === context[1] &&
    item.owner.index.context_token === context[2] && item.node.name.value === selected.node.name.value);
  if (selected.definition) {
    const ownerKey = documentKey(call.identity);
    const matches = definitions.filter((item) => documentKey(item.owner.identity) === ownerKey &&
      item.node.name.loc.start === selected.node.name.loc.start);
    return matches.length === 1 ? { name: selected.node.name.value, target: matches[0],
      selected: { node: selected.node.name, owner: call }, context } : null;
  }
  return definitions.length === 1 ? { name: selected.node.name.value, target: definitions[0],
    selected: { node: selected.node.name, owner: call }, context } : null;
}

function fragmentOccurrences(bundle, symbol, includeDeclaration, calls, asts) {
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
      item.node.name.loc.start === symbol.target.node.name.loc.start);
    if (includeDeclaration && definition && documentKey(definition.owner.identity) === documentKey(call.identity)) {
      result.push({ call: definition.owner, node: definition.node.name });
    }
    gql.visit(ast, {
      FragmentSpread(node) {
        const matches = definitions.filter((item) => item.node.name.value === node.name.value);
        if (matches.length === 1 && documentKey(matches[0].owner.identity) === targetKey &&
            matches[0].node.name.loc.start === symbol.target.node.name.loc.start) {
          result.push({ call, node: node.name });
        }
      },
    });
  }
  return result;
}

function hostFragmentOccurrence(bundle, item) {
  const snapshot = moduleSource(bundle, item.call.identity.module);
  if (!snapshot) return null;
  const modules = unique(item.call.map.utf16.filter((atom) => atom.decoded.start < item.node.loc.end &&
    item.node.loc.start < atom.decoded.end).map((atom) => atom.host.module));
  if (modules.length !== 1 || modules[0].uri !== snapshot.uri || modules[0].version !== snapshot.version) return null;
  const parts = callMappedParts(item.call, item.node.loc, modules[0]);
  if (!parts) return null;
  const range = { start: parts[0].range.start, end: parts.at(-1).range.end };
  if (parts.some((part) => part.linear && item.call.source.slice(part.decoded.start, part.decoded.end) !==
      snapshot.source.slice(part.range.start, part.range.end))) return null;
  return { module: item.call.identity.module, range, name: item.node.value,
    sourceText: snapshot.source,
    positionRange: { start: positionAt(snapshot.source, range.start), end: positionAt(snapshot.source, range.end) } };
}

function callMappedParts(call, span, module) {
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

function wordRange(text, offset) {
  let start = offset;
  let end = offset;
  while (start > 0 && /[_0-9A-Za-z]/.test(text[start - 1])) start -= 1;
  while (end < text.length && /[_0-9A-Za-z]/.test(text[end])) end += 1;
  return { start, end };
}

function editableRange(bundle, call, span, snapshot) {
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

function nodeLocation(bundle, node) {
  if (!node?.loc) return [];
  const owner = bundle.response.calls.find((item) => documentKey(item.identity) === node.loc.source.name);
  if (owner) return toHost(bundle, owner, node.loc);
  const schema = bundle.request.schemas.find((item) => item.uri === node.loc.source.name && item.format === "sdl");
  return schema ? [{ module: { uri: schema.uri, version: schema.version }, range: { start: node.loc.start, end: node.loc.end } }] : [];
}

function highlightNode(node) {
  return node.kind !== gql.Kind.VARIABLE && node.name ? node.name : node.variable ?? node;
}

function outlineAst(source) {
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

function hoverInformation(schema, text, offset) {
  const position = gls.offsetToPosition(text, offset);
  let contents = gls.getHoverInformation(schema, text, position, undefined, { useMarkdown: true });
  const context = gls.getContextAtPosition(text, position, schema);
  const info = context?.typeInfo;
  if (typeof contents !== "string" || !contents || !info) return contents;
  const kind = context.token.state.kind;
  if ((kind === "Field" || kind === "AliasedField") && info.fieldDef?.args.length) {
    // GLS 5.7 omits field arguments from its hover signature.
    const field = info.fieldDef;
    const name = field.name.startsWith("__") ? field.name : `${info.parentType.name}.${field.name}`;
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
    const prefix = `\`\`\`graphql\n${info.parentType?.name}.${field}\n\`\`\``;
    if (contents.startsWith(prefix)) contents = `\`\`\`graphql\n${info.inputObjectType.name}.${field}\n\`\`\`` + contents.slice(prefix.length);
  }
  const definition = kind === "Argument" ? info.argDef : kind === "ObjectField" ? info.fieldDef : undefined;
  if (definition?.defaultValue !== undefined) {
    const value = gql.astFromValue(definition.defaultValue, definition.type);
    if (value) contents += `\n\nDefault:\n\`\`\`graphql\n${gql.print(value)}\n\`\`\``;
  }
  return contents;
}

function definitionNodes(schema, ast, fragments, offset) {
  const result = [];
  const typeInfo = new gql.TypeInfo(schema);
  let operation;
  function selected(node) {
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
        .filter((item) => item.kind === gql.Kind.FRAGMENT_DEFINITION && item.name.value === node.name.value);
      if (targets.length === 1) result.push(targets[0].name);
    },
    Variable(node) {
      if (selected(node.name)) result.push(operation?.variableDefinitions
        ?.find((item) => item.variable.name.value === node.name.value)?.variable.name);
    },
  }));
  return result.filter(Boolean);
}

function validationIssues(schema, ast, fragments, allowed) {
  const document = { kind: gql.Kind.DOCUMENT, definitions: [...ast.definitions, ...fragments] };
  const errors = gls.validateWithCustomRules(schema, document, [gql.ExecutableDefinitionsRule])
    .filter((error) => !(error.message.startsWith("Unknown directive") &&
      error.nodes?.every((node) => node.kind === gql.Kind.DIRECTIVE && allowed.has(node.name.value) &&
        !schema.getDirective(node.name.value))));
  return { errors, warnings: gql.validate(schema, document, [gql.NoDeprecatedCustomRule]) };
}

function suggestedNames(message) {
  const index = message.indexOf("Did you mean ");
  if (index < 0 || /inline fragment/i.test(message.slice(index))) return [];
  return [...message.slice(index).matchAll(/"([_A-Za-z][_0-9A-Za-z]*)"/g)].map((item) => item[1]);
}

function deprecatedReplacement(message) {
  const reason = message.split(/is deprecated\.\s*/i)[1] ?? "";
  const match = /\b(?:use|replaced\s+(?:by|with))\s+(?:`([_A-Za-z][_0-9A-Za-z]*)`|"([_A-Za-z][_0-9A-Za-z]*)"|'([_A-Za-z][_0-9A-Za-z]*)'|([_A-Za-z][_0-9A-Za-z]*)(?![_0-9A-Za-z.]))/i.exec(reason);
  return match?.slice(1).find(Boolean) ?? null;
}

function diagnosticMatches(diagnostics, code, range, message) {
  return diagnostics.some((item) => item.code === code && item.message === message &&
    item.range.start === range.start && item.range.end === range.end);
}

function suggestionActions(bundle, snapshot, diagnostics, calls) {
  const result = [];
  const fixableKinds = new Set([gql.Kind.FIELD, gql.Kind.ARGUMENT, gql.Kind.DIRECTIVE,
    gql.Kind.NAMED_TYPE, gql.Kind.OBJECT_FIELD, gql.Kind.ENUM]);
  for (const call of calls) {
    const parsed = call.ast;
    const nameNodes = (node) => fixableKinds.has(node.kind) ? [node.name ?? (node.kind === gql.Kind.ENUM ? node : null)].filter(Boolean) : [];
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
    const parentTypes = new Map();
    gql.visit(parsed, gql.visitWithTypeInfo(typeInfo, {
      Field(node) { parentTypes.set(node, typeInfo.getParentType()); },
    }));
    for (const warning of call.warnings) for (const node of warning.nodes ?? []) {
      if (node.loc?.source?.name !== documentKey(call.call.identity)) continue;
      if (node.kind !== gql.Kind.FIELD || !node.name?.loc) continue;
      const replacement = deprecatedReplacement(warning.message);
      const parent = parentTypes.get(node);
      const definition = parent?.getFields?.()[replacement];
      if (!replacement || replacement === node.name.value || !definition || definition.deprecationReason != null) continue;
      const range = editableRange(bundle, call.call, node.name.loc, snapshot);
      if (range && diagnosticMatches(diagnostics, "graphql.deprecation", range, warning.message)) {
        result.push({ title: `Replace deprecated field '${node.name.value}' with '${replacement}'`, range, replacement });
      }
    }
  }
  return unique(result);
}

function extractFragmentPlan(bundle, snapshot, selection, directives) {
  if (!boundary(snapshot.text, selection.start) || !boundary(snapshot.text, selection.end) ||
      selection.start >= selection.end) return null;
  const trivia = text => /^(?:[\s,\uFEFF]|#[^\r\n]*(?:\r\n|\r|\n|$))*$/.test(text);
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
    const matches = [];
    gql.visit(ast, gql.visitWithTypeInfo(info, {
      SelectionSet(node) {
        const selected = node.selections.filter(item => start <= item.loc.start && item.loc.end <= end);
        const overlapping = node.selections.filter(item => item.loc.start < end && start < item.loc.end);
        if (!selected.length || selected.length !== overlapping.length ||
            selected.some(item => item.kind !== gql.Kind.FIELD) ||
            !trivia(call.source.slice(start, selected[0].loc.start)) ||
            !trivia(call.source.slice(selected.at(-1).loc.end, end))) return;
        const spreads = new Set();
        for (const item of selected) gql.visit(item, { FragmentSpread(spread) { spreads.add(spread.name.value); } });
        const parent = info.getParentType();
        if (gql.isCompositeType(parent)) matches.push({ selected, parent, spreads });
      },
    }));
    if (matches.length !== 1) continue;
    const { selected, parent, spreads } = matches[0];
    const span = { start: selected[0].loc.start, end: selected.at(-1).loc.end };
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
    const pythonNames = snapshot.text.normalize("NFKC");
    let name = base, binding, suffix = 2;
    do {
      binding = name.replace(/([A-Z]+)([A-Z][a-z])/g, "$1_$2")
        .replace(/([a-z0-9])([A-Z])/g, "$1_$2").toLowerCase();
      if (!names.has(name) && !pythonNames.includes(binding)) break;
      name = `${base}${suffix++}`;
    } while (true);
    const calls = new Map(bundle.response.calls.map(item => [documentKey(item.identity), item]));
    const references = [];
    for (const spread of spreads) {
      const matches = call.fragments.filter(reference => {
        const target = calls.get(documentKey(reference.target));
        return target?.index?.primary_fragment?.name === spread &&
          reference.reference_module.uri === snapshot.uri && reference.reference_module.version === snapshot.version;
      });
      if (matches.length !== 1) { references.length = 0; break; }
      const range = matches[0].range.utf16;
      const expression = snapshot.text.slice(range.start, range.end);
      if (range.start < callRange.start || range.end > callRange.end ||
          !/^[_A-Za-z][_0-9A-Za-z]*(?:\.[_A-Za-z][_0-9A-Za-z]*)*$/.test(expression)) { references.length = 0; break; }
      references.push(expression);
    }
    if (references.length !== spreads.size) continue;
    const fragment = `fragment ${name} on ${parent.name} {\n${call.source.slice(span.start, span.end)}\n}`;
    const rewritten = `${call.source.slice(0, span.start)}...${name}${call.source.slice(span.end)}\n${fragment}`;
    const rewrittenAst = gql.parse(new gql.Source(rewritten, documentKey(call.identity)));
    if (validationIssues(schema, rewrittenAst, fragments, allowed).errors.length) continue;
    if (!Number.isInteger(context.statement_start) || !Number.isInteger(context.fragment_insertion) ||
        !boundary(snapshot.text, context.statement_start) || !boundary(snapshot.text, context.fragment_insertion) ||
        context.statement_start > callRange.start || context.fragment_insertion < callRange.start ||
        context.fragment_insertion > callRange.end || typeof context.indent !== "string" ||
        !/^[ \t]*$/.test(context.indent) || !["\n", "\r", "\r\n"].includes(context.newline) ||
        !/^[_A-Za-z][_0-9A-Za-z]*(?:\.[_A-Za-z][_0-9A-Za-z]*)*$/.test(context.factory) ||
        !/^[_A-Za-z][_0-9A-Za-z]*$/.test(binding)) continue;
    const additions = {
      new: `, fragments=[${binding}]`, new_trailing: ` fragments=[${binding}]`, populated: `${binding}, `,
      empty_list: binding, empty_tuple: `${binding},`,
    };
    const composition = additions[context.fragment_mode];
    if (!composition || (context.fragment_insertion >= host.start && context.fragment_insertion <= host.end)) continue;
    const changes = [
      { range: host, replacement: `...${name}` },
      { range: { start: context.fragment_insertion, end: context.fragment_insertion }, replacement: composition },
      { range: { start: context.statement_start, end: context.statement_start },
        replacement: `${context.indent}${binding} = ${context.factory}(source=${JSON.stringify(fragment)}, fragments=[${references.join(", ")}])${context.newline}` },
    ];
    plans.push(changes.map(item => ({ ...item, module: call.identity.module, sourceText: snapshot.text,
      positionRange: { start: positionAt(snapshot.text, item.range.start), end: positionAt(snapshot.text, item.range.end) } })));
  }
  return plans.length === 1 ? plans[0] : null;
}

/** All analysis and freshness authority is injected; this module never starts Python. */
function createLanguageFeatures({ analyze, isCurrent, isTrusted, clientDirectives = () => [], mapSnapshot = value => value }) {
  if (![analyze, isCurrent, isTrusted].every((item) => typeof item === "function")) throw new Error("Analysis, trust and freshness callbacks are required");
  async function load(snapshot, token) {
    if (!isTrusted() || cancelled(token)) return null;
    const bundle = await analyze(snapshot, token);
    if (bundle) snapshot = mapSnapshot(snapshot, bundle);
    if (!bundle || cancelled(token) || !isTrusted() || bundle.request.request_id !== bundle.response.request_id ||
      !bundle.request.modules.some((item) => item.uri === snapshot.uri && item.version === snapshot.version && item.source === snapshot.text) ||
      !await isCurrent(bundle, snapshot, token) || cancelled(token) || !isTrusted()) return null;
    return bundle;
  }
  async function finish(bundle, snapshot, token, value, fallback) {
    if (cancelled(token) || !isTrusted() || !await isCurrent(bundle, snapshot, token)) return fallback;
    return !cancelled(token) && isTrusted() ? value : fallback;
  }
  async function at(snapshot, offset, token, action, fallback) {
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
    async complete(snapshot, offset, token) {
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
    async hover(snapshot, offset, token) {
      return at(snapshot, offset, token, (bundle, call, schema, decoded, snapshot) => {
        const contents = hoverInformation(schema, call.source, decoded);
        if (!contents || (Array.isArray(contents) && !contents.length)) return null;
        const origins = toHost(bundle, call, wordRange(call.source, decoded))
          .filter((item) => item.module.uri === snapshot.uri && item.module.version === snapshot.version && item.range.start <= offset && offset <= item.range.end);
        return origins.length === 1 ? { contents, range: origins[0].range } : null;
      }, null);
    },
    async definition(snapshot, offset, token) {
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
    async references(snapshot, offset, includeDeclaration, token) {
      return at(snapshot, offset, token, (bundle, call, _schema, decoded) => {
        const calls = new Map(bundle.response.calls.map((item) => [documentKey(item.identity), item]));
        const asts = new Map();
        const symbol = fragmentSymbolAt(bundle, call, decoded, calls, asts);
        if (!symbol) return [];
        return unique(fragmentOccurrences(bundle, symbol, includeDeclaration, calls, asts)
          .map((item) => hostFragmentOccurrence(bundle, item)).filter(Boolean));
      }, []);
    },
    async prepareRename(snapshot, offset, token) {
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
    async rename(snapshot, offset, newName, token) {
      if (!/^[_A-Za-z][_0-9A-Za-z]*$/.test(newName)) return null;
      return at(snapshot, offset, token, (bundle, call, _schema, decoded) => {
        const calls = new Map(bundle.response.calls.map((item) => [documentKey(item.identity), item]));
        const asts = new Map();
        const symbol = fragmentSymbolAt(bundle, call, decoded, calls, asts);
        if (!symbol) return null;
        const [project, schema, contextToken] = symbol.context;
        const targetKey = documentKey(symbol.target.owner.identity);
        const targetPosition = symbol.target.node.name.loc.start;
        for (const candidate of bundle.response.calls) {
          const context = schemaContext(candidate);
          if (!context || context[0] !== project || context[1] !== schema || context[2] !== contextToken) continue;
          const ast = parseCall(candidate, asts);
          if (!ast) continue;
          const definitions = fragmentNodes(bundle, candidate, ast, calls, asts);
          const targetPresent = definitions.some((item) => documentKey(item.owner.identity) === targetKey &&
            item.node.name.loc.start === targetPosition);
          const collision = definitions.some((item) => item.node.name.value === newName &&
            (documentKey(item.owner.identity) !== targetKey || item.node.name.loc.start !== targetPosition));
          if (targetPresent && collision) return null;
        }
        const occurrences = fragmentOccurrences(bundle, symbol, true, calls, asts);
        const edits = occurrences.map((item) => hostFragmentOccurrence(bundle, item));
        if (!edits.length || edits.some((item) => !item)) return null;
        const uniqueEdits = unique(edits);
        return uniqueEdits.length === edits.length
          ? uniqueEdits.map(({ module, range, positionRange, sourceText }) => ({ module, range, positionRange, sourceText, replacement: newName })) : null;
      }, null);
    },
    async extractFragment(snapshot, selection, token) {
      try {
        const bundle = await load(snapshot, token);
        if (!bundle) return null;
        snapshot = mapSnapshot(snapshot, bundle);
        const plan = extractFragmentPlan(bundle, snapshot, selection, clientDirectives);
        return await finish(bundle, snapshot, token, plan, null);
      } catch { return null; }
    },
    async symbols(snapshot, token) {
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
            if (!node.name?.loc || ![gql.Kind.OPERATION_DEFINITION, gql.Kind.FRAGMENT_DEFINITION].includes(node.kind)) continue;
            const origins = toHost(bundle, call, node.loc);
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
              detail: kind === "fragment" ? `fragment on ${node.typeCondition.name.value}` : node.operation,
              kind,
              range: definitionRange,
              selectionRange,
            });
          }
        }
        return await finish(bundle, snapshot, token, unique(result), []);
      } catch { return []; }
    },
    async codeActions(snapshot, diagnostics, token) {
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
        let semantic = [];
        if (diagnostics.some((item) => ["graphql.validation", "graphql.deprecation"].includes(item.code))) {
          const validated = [];
          for (const call of bundle.response.calls) {
            const schema = schemaFor(bundle, call);
            if (!schema) continue;
            let ast;
            try { ast = gql.parse(new gql.Source(call.source, documentKey(call.identity))); }
            catch (error) { if (error instanceof gql.GraphQLError) continue; throw error; }
            const fragments = fragmentRegistry(bundle, call);
            const issues = validationIssues(schema, ast, fragments,
              new Set([...CLIENT_DIRECTIVES, ...clientDirectives(bundle, call)]));
            validated.push({ call, schema, ast: { ...ast, definitions: [...ast.definitions, ...fragments] }, ...issues });
          }
          semantic = suggestionActions(bundle, snapshot, diagnostics, validated);
        }
        return await finish(bundle, snapshot, token, [...stale, ...insertions, ...semantic], []);
      } catch { return []; }
    },
    async diagnostics(snapshot, token) {
      try {
        const bundle = await load(snapshot, token);
        if (!bundle) return [];
        snapshot = mapSnapshot(snapshot, bundle);
        const result = bundle.response.diagnostics
          .filter((item) => item.module.uri === snapshot.uri && item.module.version === snapshot.version && item.code !== "invalid-fragments")
          .map((item) => ({ range: item.range.utf16, message: item.message,
            severity: 1, code: `graphql-desk.${item.code}`, source: "GraphQL Desk" }));
        for (const call of bundle.response.calls) {
          const schema = schemaFor(bundle, call);
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
          const { errors, warnings } = validationIssues(schema, ast, fragmentRegistry(bundle, call), allowed);
          for (const [issues, severity, code] of [[errors, 1, "graphql.validation"], [warnings, 2, "graphql.deprecation"]]) {
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
        return await finish(bundle, snapshot, token, unique(result), []);
      } catch { return []; }
    },
  };
}

function registerLanguageFeatures(vscode, options) {
  let disposed = false;
  const generations = new Map();
  const service = createLanguageFeatures({ ...options,
    isCurrent: async (...args) => !disposed && await options.isCurrent(...args) && !disposed,
  });
  const selector = [{ language: "python", scheme: "file" }, { language: "python", scheme: "untitled" }];
  const snapshot = options.snapshot ?? ((document) => ({ uri: document.uri.toString(), version: document.version, text: document.getText() }));
  const range = (document, span) => new vscode.Range(document.positionAt(span.start), document.positionAt(span.end));
  const collection = vscode.languages.createDiagnosticCollection("graphql-desk");
  const markdown = (value) => {
    const content = new vscode.MarkdownString(typeof value === "string" ? value : value.value ?? "");
    content.isTrusted = false;
    content.supportHtml = false;
    return content;
  };
  const handles = [collection,
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
    vscode.languages.registerDefinitionProvider(selector, {
      async provideDefinition(document, position, token) {
        const items = await service.definition(snapshot(document), document.offsetAt(position), token);
        return items.map((item) => new vscode.Location(options.resolveUri ? options.resolveUri(item.module.uri) : vscode.Uri.parse(item.module.uri),
          new vscode.Range(item.positionRange.start, item.positionRange.end)));
      },
    }),
    vscode.languages.registerReferenceProvider(selector, {
      async provideReferences(document, position, context, token) {
        const items = await service.references(snapshot(document), document.offsetAt(position),
          Boolean(context.includeDeclaration), token);
        return items.map((item) => new vscode.Location(options.resolveUri ? options.resolveUri(item.module.uri) : vscode.Uri.parse(item.module.uri),
          new vscode.Range(item.positionRange.start, item.positionRange.end)));
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
          ["graphql-desk.persisted-hash-mismatch", "graphql-desk.persisted-id-missing",
            "graphql.validation", "graphql.deprecation"].includes(item.code));
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
          action.edit = new vscode.WorkspaceEdit();
          action.edit.replace(document.uri, range(document, item.range), item.replacement);
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
    async prepareRename(document, position, token) {
      if (disposed || cancelled(token)) return null;
      return service.prepareRename(snapshot(document), document.offsetAt(position), token);
    },
    async rename(document, position, newName, token) {
      if (disposed || cancelled(token)) return null;
      return service.rename(snapshot(document), document.offsetAt(position), newName, token);
    },
    async extractFragment(document, selection, token) {
      if (disposed || cancelled(token)) return null;
      const source = snapshot(document);
      const plan = await service.extractFragment(source, selection, token);
      // Extraction only edits its originating file; use the editor's actual URI
      // for LSP synchronization even if analysis canonicalizes filesystem case.
      if (!plan || plan.some(item => item.sourceText !== source.text || item.module.version !== source.version ||
          item.module.uri !== plan[0].module.uri)) return null;
      return plan.map(item => ({ ...item, module: { ...item.module, uri: document.uri.toString() } }));
    },
    async refresh(document, token) {
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
    close(uri) { generations.set(uri.toString(), (generations.get(uri.toString()) ?? 0) + 1); collection.delete(uri); },
    dispose() { disposed = true; generations.clear(); handles.forEach((handle) => handle.dispose()); },
  };
}

module.exports = { createLanguageFeatures, registerLanguageFeatures, toHost, toDecoded, positionAt, boundary, documentKey,
  };
