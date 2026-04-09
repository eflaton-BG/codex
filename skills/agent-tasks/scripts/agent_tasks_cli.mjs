#!/usr/bin/env node

import {
  addComment,
  addStep,
  createNode,
  deleteComment,
  deleteNode,
  deleteStep,
  fetchNode,
  fetchTree,
  moveNode,
  pingService,
  searchNodes,
  updateComment,
  updateNode,
  updateStep,
} from "../service/client.js";

function usage() {
  console.log(`Usage:
  node scripts/agent_tasks_cli.mjs health
  node scripts/agent_tasks_cli.mjs tree [--root-id <id>] [--statuses <csv>] [--active-only]
  node scripts/agent_tasks_cli.mjs show <node_id>
  node scripts/agent_tasks_cli.mjs search [--query <text>] [--kind <any|task|folder>] [--root-id <id>] [--limit <n>] [--statuses <csv>] [--active-only] [--all-statuses]
  node scripts/agent_tasks_cli.mjs create-folder [--parent-id <id>] [--summary <text>]
  node scripts/agent_tasks_cli.mjs create-task [--parent-id <id>] [--summary <text>]
  node scripts/agent_tasks_cli.mjs move <node_id> [--parent-id <id> | --root] --insert-index <n>
  node scripts/agent_tasks_cli.mjs update <node_id> [--summary <text>] [--status <status>] [--priority <priority>] [--due-at <iso>]
  node scripts/agent_tasks_cli.mjs delete <node_id>
  node scripts/agent_tasks_cli.mjs add-step <node_id> [text]
  node scripts/agent_tasks_cli.mjs update-step <step_id> [--text <text>] [--status <ready|blocked|working|complete>] [--position <n>]
  node scripts/agent_tasks_cli.mjs delete-step <step_id>
  node scripts/agent_tasks_cli.mjs add-comment <node_id> [body]
  node scripts/agent_tasks_cli.mjs update-comment <comment_id> [body]
  node scripts/agent_tasks_cli.mjs delete-comment <comment_id>

Notes:
  --active-only keeps nodes with statuses new, ready, blocked, working, or review,
    and excludes complete/aborted work unless a matching descendant still needs to
    keep a parent folder visible.
  search defaults to active-only filtering; tree only uses it when you pass --active-only.`);
}

function parseFlags(entries) {
  const payload = {};
  for (let index = 0; index < entries.length; index += 2) {
    const flag = entries[index];
    const value = entries[index + 1] ?? "";
    if (!flag?.startsWith("--")) {
      continue;
    }
    const key = flag.slice(2).replace(/-/g, "_");
    payload[key] = value;
  }
  return payload;
}

function parsePositiveInteger(value, flagName) {
  const trimmed = String(value ?? "").trim();
  if (!/^\d+$/.test(trimmed)) {
    throw new Error(`Invalid ${flagName}: expected a positive integer`);
  }
  const parsed = Number(trimmed);
  if (!Number.isSafeInteger(parsed) || parsed <= 0) {
    throw new Error(`Invalid ${flagName}: expected a positive integer`);
  }
  return parsed;
}

function parseNonNegativeInteger(value, flagName) {
  const trimmed = String(value ?? "").trim();
  if (!/^\d+$/.test(trimmed)) {
    throw new Error(`Invalid ${flagName}: expected a non-negative integer`);
  }
  const parsed = Number(trimmed);
  if (!Number.isSafeInteger(parsed) || parsed < 0) {
    throw new Error(`Invalid ${flagName}: expected a non-negative integer`);
  }
  return parsed;
}

function parseStepStatus(value) {
  const normalized = String(value ?? "").trim();
  if (!["ready", "blocked", "working", "complete"].includes(normalized)) {
    throw new Error("Invalid --status: expected one of ready, blocked, working, complete");
  }
  return normalized;
}

function parseKind(value) {
  const normalized = String(value ?? "").trim() || "any";
  if (!["any", "task", "folder"].includes(normalized)) {
    throw new Error("Invalid --kind: expected any, task, or folder");
  }
  return normalized;
}

function parseTreeArgs(entries) {
  const payload = {};
  for (let index = 0; index < entries.length; index += 1) {
    const entry = entries[index];
    if (entry === "--root-id") {
      payload.root_id = parsePositiveInteger(entries[index + 1], "--root-id");
      index += 1;
      continue;
    }
    if (entry === "--statuses") {
      payload.statuses = entries[index + 1] ?? "";
      index += 1;
      continue;
    }
    if (entry === "--active-only") {
      payload.active_only = true;
      continue;
    }
    throw new Error(`Unknown argument: ${entry}`);
  }
  return payload;
}

function parseSearchArgs(entries) {
  const payload = { active_only: true, kind: "any", limit: 20 };
  for (let index = 0; index < entries.length; index += 1) {
    const entry = entries[index];
    if (entry === "--query") {
      payload.query = entries[index + 1] ?? "";
      index += 1;
      continue;
    }
    if (entry === "--kind") {
      payload.kind = parseKind(entries[index + 1]);
      index += 1;
      continue;
    }
    if (entry === "--root-id") {
      payload.root_id = parsePositiveInteger(entries[index + 1], "--root-id");
      index += 1;
      continue;
    }
    if (entry === "--limit") {
      payload.limit = parsePositiveInteger(entries[index + 1], "--limit");
      index += 1;
      continue;
    }
    if (entry === "--statuses") {
      payload.statuses = entries[index + 1] ?? "";
      payload.active_only = false;
      index += 1;
      continue;
    }
    if (entry === "--active-only") {
      payload.active_only = true;
      continue;
    }
    if (entry === "--all-statuses") {
      delete payload.statuses;
      payload.active_only = false;
      continue;
    }
    throw new Error(`Unknown argument: ${entry}`);
  }
  return payload;
}

function parseCreateArgs(entries, defaults) {
  const payload = { ...defaults };
  for (let index = 0; index < entries.length; index += 1) {
    const entry = entries[index];
    if (entry === "--parent-id") {
      payload.parent_id = parsePositiveInteger(entries[index + 1], "--parent-id");
      index += 1;
      continue;
    }
    if (entry === "--summary") {
      payload.summary = entries[index + 1] ?? "";
      index += 1;
      continue;
    }
    throw new Error(`Unknown argument: ${entry}`);
  }
  return payload;
}

function parseMoveArgs(entries) {
  const payload = {};
  let sawParentId = false;
  let sawRoot = false;
  for (let index = 0; index < entries.length; index += 1) {
    const entry = entries[index];
    if (entry === "--parent-id") {
      payload.parent_id = parsePositiveInteger(entries[index + 1], "--parent-id");
      sawParentId = true;
      index += 1;
      continue;
    }
    if (entry === "--root") {
      payload.parent_id = null;
      sawRoot = true;
      continue;
    }
    if (entry === "--insert-index") {
      payload.insert_index = parseNonNegativeInteger(entries[index + 1], "--insert-index");
      index += 1;
      continue;
    }
    throw new Error(`Unknown argument: ${entry}`);
  }
  if (sawParentId && sawRoot) {
    throw new Error("Cannot use --parent-id and --root together");
  }
  if (payload.insert_index === undefined) {
    throw new Error("Missing --insert-index");
  }
  if (!sawParentId && !sawRoot) {
    payload.parent_id = null;
  }
  return payload;
}

function parseUpdateArgs(entries) {
  const payload = {};
  for (let index = 0; index < entries.length; index += 1) {
    const entry = entries[index];
    if (entry === "--summary") {
      payload.summary = entries[index + 1] ?? "";
      index += 1;
      continue;
    }
    if (entry === "--status") {
      payload.status = entries[index + 1] ?? "";
      index += 1;
      continue;
    }
    if (entry === "--priority") {
      payload.priority = entries[index + 1] ?? "";
      index += 1;
      continue;
    }
    if (entry === "--due-at") {
      payload.due_at = entries[index + 1] ?? "";
      index += 1;
      continue;
    }
    throw new Error(`Unknown argument: ${entry}`);
  }
  return payload;
}

async function main() {
  const args = process.argv.slice(2);
  const [command, ...rest] = args;

  if (!command) {
    usage();
    process.exit(1);
  }

  let payload;
  if (command === "health") {
    payload = await pingService();
  } else if (command === "tree") {
    payload = await fetchTree(parseTreeArgs(rest));
  } else if (command === "show") {
    payload = await fetchNode(parsePositiveInteger(rest[0], "node_id"));
  } else if (command === "search") {
    payload = await searchNodes(parseSearchArgs(rest));
  } else if (command === "create-folder") {
    payload = await createNode(parseCreateArgs(rest, { kind: "folder", parent_id: null, summary: "New Folder" }));
  } else if (command === "create-task") {
    payload = await createNode(parseCreateArgs(rest, { kind: "task", parent_id: null, summary: "New Task" }));
  } else if (command === "move") {
    payload = await moveNode(parsePositiveInteger(rest[0], "node_id"), parseMoveArgs(rest.slice(1)));
  } else if (command === "update") {
    payload = await updateNode(Number(rest[0]), parseUpdateArgs(rest.slice(1)));
  } else if (command === "delete") {
    payload = await deleteNode(Number(rest[0]));
  } else if (command === "add-step") {
    payload = await addStep(Number(rest[0]), { text: rest.slice(1).join(" ") || "New step" });
  } else if (command === "update-step") {
    const flags = parseFlags(rest.slice(1));
    if (flags.status !== undefined) {
      flags.status = parseStepStatus(flags.status);
    }
    if (flags.position !== undefined) {
      flags.position = parsePositiveInteger(flags.position, "--position");
    }
    payload = await updateStep(Number(rest[0]), flags);
  } else if (command === "delete-step") {
    payload = await deleteStep(Number(rest[0]));
  } else if (command === "add-comment") {
    payload = await addComment(Number(rest[0]), { body: rest.slice(1).join(" ") || "New comment", author_type: "agent" });
  } else if (command === "update-comment") {
    payload = await updateComment(Number(rest[0]), { body: rest.slice(1).join(" ") || "Updated comment" });
  } else if (command === "delete-comment") {
    payload = await deleteComment(Number(rest[0]));
  } else {
    usage();
    process.exit(1);
  }

  console.log(JSON.stringify(payload, null, 2));
}

main().catch((error) => {
  console.error(error instanceof Error ? error.message : String(error));
  process.exit(1);
});
