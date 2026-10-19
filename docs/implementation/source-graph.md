# Static module source graph

`graphql_desk.source_graph.resolve_project(modules)` consumes a tuple of caller-selected `ModuleSnapshot(project, module, uri, version, source, is_package=False)` facts. It never discovers files, imports project modules or executes Python. Module identity and URI must be unique within a project; project boundaries isolate identical module names.

The initial slice resolves stable module-level GraphQL Desk callable aliases, string constants, literal `+`, imports, relative imports, namespace access and transitive reexports. Each resolved call carries its source text, optional selected operation/binding, host call span, contributing literal spans and module URI/version dependencies. A fresh input snapshot recomputes facts; there is no persistent index or cache. Literal spans identify contributing expressions, not exact decoded-character mappings.

The resolver rejects ambiguous/rebound or mutation-dependent bindings and reports unsupported static-source cases at known GraphQL call sites. Ordinary imported Python functions and local lookalike modules do not become GraphQL candidates. Namespace reflection and escaped module objects may prevent a stability proof and must not yield successful precise facts.

This is a separate backend from the existing one-module `extract_calls` API, whose behavior remains unchanged. The new backend intentionally diagnoses recognized calls in nested scopes; generated/configured factories, schema initialization and fragment bindings remain the next #28 slice. Exact decoded maps remain #29. Project discovery/source matching, fragment indexing, literal-preserving checker inference and live editor integration are not established by this graph. Broad SRC-01 and live-typing gates remain open.
