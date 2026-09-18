#!/usr/bin/env node
// Builds the devcontainer.json that `devcontainer up` is actually run with.
//
// Why this exists: the agent container talks to a *remote* daemon (the socket
// proxy), so the devcontainer CLI's default workspace bind mount would be
// resolved against that daemon's filesystem and point at nothing. The job
// volume is the only thing both sides can name, so the effective config gets an
// explicit `workspaceMount` onto that volume. Everything else in a repository's
// own devcontainer.json is left untouched.
//
// devcontainer.json is JSON with comments, hence jsonc-parser rather than JSON.parse.

import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { basename, dirname, join } from "node:path";
import { existsSync } from "node:fs";
import { parse, printParseErrorCode } from "jsonc-parser";

function usage(message) {
  process.stderr.write(
    `${message}\n\n` +
      "usage: devcontainer-config.mjs --workspace-folder <dir> --out <file>\n" +
      "                               [--volume <name>] [--mount-target <dir>]\n" +
      "                               [--default-image <ref>]\n",
  );
  process.exit(2);
}

const args = new Map();
for (let i = 2; i < process.argv.length; i += 2) {
  const key = process.argv[i];
  if (!key.startsWith("--")) usage(`unexpected argument: ${key}`);
  args.set(key.slice(2), process.argv[i + 1]);
}

const workspaceFolder = args.get("workspace-folder");
const out = args.get("out");
if (!workspaceFolder || !out) usage("--workspace-folder and --out are required");

const volume = args.get("volume");
const mountTarget = args.get("mount-target") ?? "/workspaces";
const defaultImage = args.get("default-image") ?? "mcr.microsoft.com/devcontainers/base:bookworm";

const candidates = [
  join(workspaceFolder, ".devcontainer", "devcontainer.json"),
  join(workspaceFolder, ".devcontainer.json"),
];
const source = candidates.find((candidate) => existsSync(candidate));

let config;
if (source) {
  const errors = [];
  config = parse(readFileSync(source, "utf8"), errors, { allowTrailingComma: true });
  if (errors.length > 0) {
    const detail = errors
      .map((error) => `${printParseErrorCode(error.error)} at offset ${error.offset}`)
      .join(", ");
    process.stderr.write(`agent: cannot parse ${source}: ${detail}\n`);
    process.exit(1);
  }
  if (config?.dockerComposeFile) {
    // A compose-based devcontainer brings its own mounts, and rewriting them
    // here would mean editing a file the repository owns. Refuse clearly
    // instead of producing a container whose workspace is silently empty.
    process.stderr.write(
      "agent: compose-based devcontainers are not supported yet, because the job volume " +
        "cannot be injected into a devcontainer.json that delegates to docker-compose.\n",
    );
    process.exit(1);
  }
} else {
  config = { name: "agent-sandbox", image: defaultImage };
}

if (volume) {
  config.workspaceMount = `source=${volume},target=${mountTarget},type=volume`;
  config.workspaceFolder = join(mountTarget, basename(workspaceFolder));
}

// Written next to the repository's own config so that relative paths inside it
// — a `build.dockerfile`, a `context` — keep resolving the way the repo meant.
const outDir = dirname(out);
mkdirSync(outDir, { recursive: true });
writeFileSync(out, `${JSON.stringify(config, null, 2)}\n`, "utf8");

process.stdout.write(
  source ? `agent: using devcontainer config from ${source}\n` : "agent: no devcontainer.json in repository, using default image\n",
);
