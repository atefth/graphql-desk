# Optional saved-model evaluation: Turms 2.3.0

This records the bounded **evaluation** for #85, not a generator integration or
live-inference feature. Only Turms ran. The fixture, full hashed dependency lock,
stdlib runner, assertions, generated output and recorded evidence live in
[`examples/saved-generator-evaluation`](../../examples/saved-generator-evaluation).
No GraphQL Desk package dependency or shared environment was changed.

## Result and limits

The fixture passed on **CPython 3.13.7 / Windows 11**. Turms 2.3.0 ran against the
core's `graphql-core==3.3.1`. Its [published metadata](https://pypi.org/pypi/turms/2.3.0/json)
declares MIT, Python >=3.10 and graphql-core >=3.2,<4. The locked wheel SHA-256 is
`d2269c099522e12612bf825c8dfc63bce603349874bed3506a630a0fcd513bd9`;
the install used `--require-hashes`. The runner verifies installed versions against
the lock; it does not retrospectively prove an arbitrary installation's origin.

The isolated resolution has 24 distributions, including Pydantic 2.14.3,
pydantic-core 2.50.4 and pydantic-settings 2.16.2. The latter
[requires Python >=3.11](https://pypi.org/pypi/pydantic-settings/2.16.2/json), so this
lock is **not** a Python 3.10 compatibility claim. Pydantic and pydantic-core report
MIT; the complete declared-license inventory is in `evidence.json`, including
certifi's MPL-2.0 and packages reporting only license classifiers. This is metadata
inventory, not a new release/security audit. No formatter, funcs/client plugin,
processor or watch command ran; watchfiles is an unused transitive dependency.

| Fixture | Observed result |
| --- | --- |
| Valid response; wrong type; missing required result/variable fields | Valid model constructs; invalid data raises Pydantic ValidationError |
| Invalid GraphQL field | Generation exits nonzero and produces no output |
| Two generations | Identical bytes; normalized source matches `expected_schema.txt` |
| GraphQL aliases | `displayName` becomes `display_name`; wire output requires `by_alias=True` |
| Omitted versus explicit null | `exclude_unset=True` omits unset keys while retaining explicit `None` |
| Enum and DateTime | Strings become enum/datetime objects; `mode="json"` restores wire values |
| Coercion and identity | Input `limit="5"` becomes integer 5; response dictionaries stay unchanged but become new model objects |
| Copy behavior | Model copy creates a new outer object; shallow copy shares nested models, deep copy does not |

Generated runtime imports are only stdlib modules and Pydantic; they do not import
Turms or GraphQL Desk. These validation, coercion, serialization and copying
behaviors are materially different from core identity-preserving typed documents.
Turms could support a separately chosen saved DTO/model workflow, with explicit
conversion at its boundary. It is not a transparent core backend or a substitute
for live Pylance typing, fragment masking or parity acceptance. No adapter, client,
transport, network schema fetch or automatic regeneration is delivered here.

Ariadne Codegen 0.19.0 was not installed or executed. Its
[graphql-core <3.3 requirement](https://pypi.org/pypi/ariadne-codegen/0.19.0/json)
conflicts with the core pin; this evaluation neither downgrades core nor compares
two backends. Further saved-generator adoption remains demand-driven and optional.

## Reproduce in an isolated environment

From the repository root in PowerShell, with the existing primary Python 3.13:

```powershell
$task = Join-Path (Get-Location) '.tmp/issue85-eval'
$temporary = Join-Path (Get-Location) '.tmp'
if (Get-Item -LiteralPath $task -Force -ErrorAction SilentlyContinue) {
    throw 'Task directory already exists. Use the evaluate-only replay below.'
}
$temporaryInfo = Get-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
if ($temporaryInfo -and ($temporaryInfo.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
    throw 'The task parent must not be redirected.'
}
New-Item -ItemType Directory -Path $task -ErrorAction Stop | Out-Null
$env:TEMP = $task
$env:TMP = $task
$env:UV_CACHE_DIR = Join-Path $task 'uv-cache'
$env:UV_PYTHON_INSTALL_DIR = Join-Path $task 'python'
$env:UV_PYTHON_DOWNLOADS = 'never'
$env:PYTHONDONTWRITEBYTECODE = '1'
uv venv --python .venv/Scripts/python.exe .tmp/issue85-eval/environment
uv pip install --python .tmp/issue85-eval/environment/Scripts/python.exe --only-binary :all: --require-hashes -r examples/saved-generator-evaluation/requirements.lock
.tmp/issue85-eval/environment/Scripts/python.exe -I -B -X utf8 examples/saved-generator-evaluation/evaluate.py
```

These provisioning commands require an entirely absent task directory. They
deliberately refuse any existing task/environment before `uv venv`, which would
otherwise replace an existing virtual environment. **Do not provision again to
replay this evaluation, especially while cleanup of that environment is blocked.**

For an already provisioned environment, replay only the evaluator with a fresh
output name; there are no provisioning or cleanup commands in this recipe:

```powershell
.tmp/issue85-eval/environment/Scripts/python.exe -I -B -X utf8 examples/saved-generator-evaluation/evaluate.py --output review-result
```

The runner refuses an existing result directory and redirected task directories;
resolved task/output locations must remain inside the real repository. A fresh
`--output` name preserves another review result inside that task directory.
It clears inherited Turms/Python overrides,
requires Python 3.13 and an isolated prefix, and checks core is absent there.
Temporary fixture directories are removed on success or failure; logs and JSON
evidence remain in the selected result directory. After preserving evidence,
remove only the task-created environment/cache after checking resolved paths,
ownership and reparse points. Do not remove unrelated work or junction targets.

CI reuses the Ubuntu Python 3.13 smoke lane when the example or workflow changes.
It checks path guards before creating a fresh isolated environment, installs with
this lock's hashes, runs the same evaluator and verifies core still cannot import
Turms or Pydantic. The recorded local evidence remains Windows-only until that
hosted run passes; other platforms and Python minors remain unverified.

Local cleanup status: the runner removed its temporary per-run fixtures. Automatic
approval review rejected the separate removal of the task-created `environment`,
`uv-cache` and initial manual `fixture` directories under `.tmp/issue85-eval`.
Those directories remain (about 64 MB logical size); this audit does not claim
their cleanup succeeded. No deletion was retried through another mechanism.
