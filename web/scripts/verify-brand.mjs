#!/usr/bin/env node

import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const webRoot = resolve(here, "..");
const repoRoot = resolve(webRoot, "..");

const required = new Map([
  ["index.html", ["#0B0C0D"]],
  ["public/manifest.webmanifest", ['"background_color": "#0B0C0D"', '"theme_color": "#0B0C0D"']],
  ["public/sw.js", ['const CACHE = "helios-v5"']],
  ["src/index.css", ["--bg: #0B0C0D", "--alert: #CB5B45"]],
  ["tailwind.config.js", ['bg: "#0B0C0D"', 'alert: "#CB5B45"']],
  ["../design/tokens.json", ['"version": "2.1 (Ink and Bone v1.1.0)"', '"bg": "#0B0C0D"', '"alert": "#CB5B45"']],
]);

const banned = [
  "#0B0D0C",
  "#151917",
  "#232826",
  "#7EE0B1",
  "#9AA49E",
  "#E8ECE9",
  "#F87171",
  "#FBBF24",
];
const failures = [];

function text(path) {
  return readFileSync(resolve(webRoot, path), "utf8");
}

for (const [path, needles] of required) {
  const contents = text(path);
  for (const needle of needles) {
    if (!contents.includes(needle)) {
      failures.push(`${path}: missing ${needle}`);
    }
  }
}

const activeInputs = [
  "index.html",
  "public/manifest.webmanifest",
  "src",
  "tailwind.config.js",
  "../design/tokens.json",
];

function filesBelow(path) {
  const absolute = resolve(webRoot, path);
  if (!existsSync(absolute)) return [];
  if (!statSync(absolute).isDirectory()) return [absolute];
  return readdirSync(absolute, { withFileTypes: true }).flatMap((entry) => {
    const child = join(absolute, entry.name);
    return entry.isDirectory() ? filesBelow(child) : [child];
  });
}

for (const file of activeInputs.flatMap(filesBelow)) {
  const contents = readFileSync(file, "utf8").toUpperCase();
  for (const hex of banned) {
    if (contents.includes(hex)) {
      failures.push(`${relative(repoRoot, file)}: retired live value ${hex}`);
    }
  }
}

const dist = resolve(webRoot, "dist");
if (existsSync(dist)) {
  for (const file of filesBelow(dist).filter((path) => /\.(css|html|js|json|webmanifest)$/.test(path))) {
    const contents = readFileSync(file, "utf8").toUpperCase();
    for (const hex of banned) {
      if (contents.includes(hex)) {
        failures.push(`${relative(repoRoot, file)}: retired built value ${hex}`);
      }
    }
  }
}

if (failures.length) {
  console.error("Helios brand verification failed:\n" + failures.map((line) => `- ${line}`).join("\n"));
  process.exit(1);
}

console.log("Helios active sources and built text assets use the current Ink and Bone palette.");
