"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { downloadAndUnzipVSCode } = require("@vscode/test-electron");

const editor = fs.realpathSync(path.resolve(__dirname, ".."));
const cache = path.join(editor, ".cache");
const cachePath = path.join(cache, "vscode-test");
for (const directory of [cache, cachePath]) {
  fs.mkdirSync(directory, { recursive: true });
  if (fs.realpathSync(directory) !== directory) throw new Error("VS Code cache must stay inside the editor directory");
}
const temp = fs.mkdtempSync(path.join(cachePath, "download-"));
Object.assign(process.env, { TEMP: temp, TMP: temp, TMPDIR: temp });

downloadAndUnzipVSCode({
  version: process.argv[2],
  cachePath,
  reporter: { report() {}, error(error) { console.error(error); } },
}).finally(() => {
  if (fs.realpathSync(temp) !== temp) throw new Error("VS Code download temporary directory moved");
  fs.rmSync(temp, { recursive: true });
}).then(executable => process.stdout.write(executable)).catch(error => {
  console.error(error);
  process.exitCode = 1;
});
