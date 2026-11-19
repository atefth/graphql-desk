# Doctor health reports (#65)

The shared Python collector inspects setup without executing project Python or
writing generated output. `collect_project_health` uses an explicit directory,
optional configuration/schema selection, and explicitly supplied environment.
It reads saved configuration, checks the selected and nearest ancestor project's
generated ownership, loads schemas, and resolves mapping declarations using the
same static source/stub capture as the existing CLI doctor. A requested remote
schema load is explicit; imports and captured-editor diagnosis never fetch.

`collect_snapshot_health` reuses the worker's captured project configurations,
validated schema contexts, and mapping modules. It does not reread schemas or
project modules. Its required `configuration_diagnostics` keyword receives the
bound preparation's complete diagnostic tuple, so one malformed project cannot
disappear behind another valid project. The trusted caller must bind those facts
to the same current worker snapshot. A captured report is not a publish or
freshness permit.

Reports contain named checks with `ready`, `degraded`, or `unavailable` status,
sanitized actions, and the reporting process's executable, Python version, and
installed distribution versions. Static symbol availability does not establish
checker assignability or runtime identity. Explicit `typing.Any` mappings remain
visible. Failed checks do not repeat schema text, configured URLs, headers, or
backend exceptions.

The standalone terminal doctor has no authoritative live editor connection.
Its report remains degraded with nonzero status when editor, checker, ownership,
or bridge health is unavailable. A generated manifest, owner JSON, timestamp, or
workspace health file cannot establish an active editor session. This component
does not add a health file, discovery daemon, or another IPC service.

The editor-owned `collectEditorDoctor` composer receives private callbacks from
the active extension. `workerHealth` obtains checks from its selected Python
worker; `verifyPublication` verifies actual owned artifacts and effective import
selection against that session's publication; `isCurrent` revalidates that same
source, buffer, configuration, schema, credential, and interpreter observation.
The caller must return true only after its existing native snapshot/currentness
checks and live-session guards pass. The composer revalidates the worker capture
before and after collecting health and verifying publication. After the last
worker check, it rechecks successful ownership, then checks trust, workspace
membership and active versions before returning. No result is cached or written
to disk.

Only the live host supplies VS Code and active GraphQL Desk, Python, and Pylance
extension versions, through their public metadata. An installed Pyright or mypy
distribution is reported separately from the active checker. Pylance activation
does not acknowledge consumption of a specific generation: a healthy bridge row
means the current inputs have a verified selected publication in this session.
It does not turn a setup diagnosis into a full type-checking or parity proof.

The editor composer ignores live-authority rows received in a Python report and
replaces them using its private currentness/ownership callbacks and live host
metadata. Missing extensions, incomplete configuration, unknown ownership,
unavailable callbacks, stale observations, trust loss, or provider failure produce
nonzero status and a redacted action. Callback failures cannot echo private
details. Currentness is an observation when the report completes, not a universal
filesystem compare-and-swap guarantee or a promise about later edits.

The terminal command uses this collector and preserves schema, explicit-Any,
local/interpreter mapping, redaction and degraded-exit diagnostics. It prints
results after collection; cancellation exits with status 130 without publishing a
partial report. Its explicitly requested saved-schema inspection may access a
configured URL using the supplied environment; a captured worker report never
fetches a schema or executes project Python.

The read-only worker `doctor` operation requires the same prepared and analyzed
job, source/configuration/schema buffers, and remote credential revisions as
`current` and `artifacts`. It binds the preparation's configuration diagnostics,
checks currentness before and after collection, and returns
`{ok: true, id, current: true, report}` only while that capture remains current.
A stale observation clears the job and returns `{ok: true, id, current: false}`
without a report. Malformed or mismatched jobs retain the worker's sanitized
failure contract. This report does not acknowledge that Pylance consumed the
published types. Pinned installed-editor acceptance and the complete story and
broad CLI/typing gates remain separate requirements.

`GraphQL Desk: Doctor` uses the selected worker and revalidates that same analyzed
job around report collection. It does not initialize a managed directory, update
configuration, write a health file, or enable remote polling. A later ordinary
analysis/publication or explicit refresh retains its existing polling behavior.
The command checks trust, workspace membership and disposal again after awaiting
the report, before displaying or returning successful observations.

The editor accepts generated ownership only from a live publisher verification
capability plus the effective selected setting and matching analysis/input
identity. The generation publisher records a session witness after selecting a
generation or verifying that its existing selection has identical inputs and
outputs. Doctor rechecks the owned setting, generation directory and manifest
identities, and complete artifact hashes without writing files. A missing
witness leaves ownership unavailable, and a quarantine generation cannot attest
to the current precise bridge. For an explicit workspace-local stub path, Doctor
verifies the complete in-place publication through its live output handles and
checks that the effective settings and native Pyright route still select it.
For workspace-settings routes, a possible parent Pyright configuration leaves
selection unverified. Doctor checks only whether those candidate paths exist.
Per-file nearest-configuration mode also leaves the root-level proof unverified.
Pylance version metadata remains separate from evidence that the checker
consumed the published types.

## Reproduce the custom stub path check

Start a trusted single-folder workspace with strict Pylance checking. Choose a
directory whose parents contain neither `pyrightconfig.json` nor `pyproject.toml`.
Select the Python environment with GraphQL Desk installed, set the workspace's
`python.analysis.stubPath` to `custom-stubs`, and place a text file of your own
in that folder. Use these three project files.

`pyproject.toml`

```toml
[tool.graphql-desk]
schema = "schema.graphql"
schema_output = "graphql_desk_generated/default.pyi"
```

`schema.graphql`

```graphql
type Query { selected: String! count(limit: Int!): Int! }
```

`consumer.py`

```python
from typing import TypeVar

from graphql_desk import TypedDocument
from graphql_desk_generated.default import graphql

R = TypeVar("R")
V = TypeVar("V")

def execute(document: TypedDocument[R, V], variables: V) -> R:
    raise NotImplementedError

doc = graphql("query Pick($limit: Int!) { selected count(limit: $limit) }")
selected = execute(doc, {"limit": 1})["selected"]
wrong = execute(doc, {"limit": "bad"})
```

Open the Python file. After publication, hover over `selected`, go to the
definition of the imported `graphql`, and inspect the diagnostic on `wrong`.
Run **GraphQL Desk: Doctor** from the command palette. Run `graphql-desk doctor`
from the same workspace in a terminal for the standalone case.

On Windows, the isolated extension-host runs at commit `a7b6d7f` observed the
following results with VS Code 1.141.0 and 1.142.0, Pylance 2026.4.1, and Python
3.13.7. Each host loaded the compiled extension from its extension directory.

| Observation | Result on both editor versions |
| --- | --- |
| Pylance hover and definition | `selected: String`; factory definition inside `custom-stubs/graphql_desk_generated/default.py` |
| Variable diagnostic | `reportArgumentType` on the string passed for the required integer |
| Editor Doctor | Exit 0. Configuration, project and generated ownership, schema, active editor and checker, and live bridge all ready |
| Doctor side effects | Zero workspace changes, ownership-state updates, or Pylance restarts during the command |
| Extension deactivation | Original workspace contents, neighboring file, and custom setting preserved |

In the terminal run, configuration and schema were ready. Doctor identified the
selected Python, reported that the active editor, checker and bridge were
unavailable, directed the operator to the editor command, and exited 1.
The packaged release and large-project performance have separate acceptance
issues. [Doctor acceptance](https://github.com/atefth/graphql-desk/issues/65)
