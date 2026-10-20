# Scalar identity helpers

Root `graphql.scalar(name, value)`, its `gql` alias and `init_graphql(...).scalar`
return the identical value. They do not load schemas, coerce values, add keys or
validate data at runtime. The root constructor remains the same ordinary Python
function; attaching the member preserves its call signature and aliases.

Generated schema factories expose scalar overloads through their callable
protocol, alongside the existing document overloads. Known scalar, enum and input
object names accept their configured input types and return those input types;
explicit `None` stays `None`. Configured built-in mappings take precedence,
including specified scalars unused by the schema. There is no unknown-name,
wrong-value or omitted-value fallback in the generated method. Unbound root and
runtime-only factories remain schema-agnostic generic identity helpers.

`scalar_types.render_scalar_method` returns method declarations only. The enclosing
factory supplies its existing schema aliases and private typing imports once.
Rendering imports no configured project module; ordinary generated declaration
imports retain the shared mapping policy and its unresolved-symbol limitations.

Tests check original function/value identities, generated original-source calls
and both strict checkers, including recursive inputs, oneOf alternatives, enum
mapping, distinct input/output types, unused built-ins and invalid calls. This
implements the scalar member foundation, not static project symbol resolution,
the complete API surface or a broad live-typing acceptance gate.
