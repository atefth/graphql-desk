import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { spawnSync } from 'node:child_process';
import assert from 'node:assert/strict';

const dir = dirname(fileURLToPath(import.meta.url));
const cli = resolve(dir, 'node_modules/graphql-language-service-cli/bin/graphql.js');
const schema = resolve(dir, 'schema.graphql');
const run = (...args) => spawnSync(process.execPath, [cli, ...args], { cwd: dir, encoding: 'utf8' });

const invalid = run('validate', '--text', 'query UserCard { user { missingField } }', '--schemaPath', schema);
assert.equal(invalid.status, 0, invalid.stderr);
const diagnostics = Object.values(JSON.parse(invalid.stdout));
const missingField = diagnostics.find(({ message }) => message.includes('missingField'));
assert.deepEqual(missingField.range, {
  start: { line: 0, character: 24 },
  end: { line: 0, character: 37 },
});

const completion = run('autocomplete', '--text', 'query UserCard { user { na', '--schemaPath', schema, '--row', '0', '--column', '27');
assert.equal(completion.status, 0, completion.stderr);
assert.ok(Object.values(JSON.parse(completion.stdout)).some(({ label }) => label === 'name'));

console.log('GraphQL service returns the expected unknown-field range and schema completion.');
