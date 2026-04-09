#!/usr/bin/env node

import {
  createNote,
  createProject,
  deleteNote,
  deleteProject,
  getNote,
  listNotes,
  listProjects,
  pingService,
  updateNote,
  updateProject,
} from "../service/client.js";

function usage() {
  console.log(`Usage:
  node scripts/agent_notes_cli.mjs health
  node scripts/agent_notes_cli.mjs projects list
  node scripts/agent_notes_cli.mjs projects search <query>
  node scripts/agent_notes_cli.mjs projects create [name]
  node scripts/agent_notes_cli.mjs projects rename <id> <name>
  node scripts/agent_notes_cli.mjs projects delete <id>
  node scripts/agent_notes_cli.mjs notes list <project_id>
  node scripts/agent_notes_cli.mjs notes search <project_id> <query>
  node scripts/agent_notes_cli.mjs notes create <project_id> [title]
  node scripts/agent_notes_cli.mjs notes get <note_id>
  node scripts/agent_notes_cli.mjs notes update <note_id> [--title <title>] [--description <description>]
  node scripts/agent_notes_cli.mjs notes delete <note_id>`);
}

function normalizeSearchText(value) {
  return String(value ?? "")
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "");
}

function matchesSearch(query, ...values) {
  const normalizedQuery = normalizeSearchText(query);
  if (!normalizedQuery) {
    return true;
  }
  return values.some((value) => normalizeSearchText(value).includes(normalizedQuery));
}

async function main() {
  const args = process.argv.slice(2);
  const [domain, action, ...rest] = args;

  if (!domain) {
    usage();
    process.exit(1);
  }

  let payload;
  if (domain === "health") {
    payload = await pingService();
  } else if (domain === "projects" && action === "list") {
    payload = await listProjects();
  } else if (domain === "projects" && action === "search") {
    const query = rest.join(" ").trim();
    const response = await listProjects();
    payload = {
      projects: response.projects.filter((project) => matchesSearch(query, project.name)),
    };
  } else if (domain === "projects" && action === "create") {
    payload = await createProject({ name: rest.join(" ").trim() || undefined });
  } else if (domain === "projects" && action === "rename") {
    payload = await updateProject(Number(rest[0]), { name: rest.slice(1).join(" ") });
  } else if (domain === "projects" && action === "delete") {
    payload = await deleteProject(Number(rest[0]));
  } else if (domain === "notes" && action === "list") {
    payload = await listNotes(Number(rest[0]));
  } else if (domain === "notes" && action === "search") {
    const projectId = Number(rest[0]);
    const query = rest.slice(1).join(" ").trim();
    const response = await listNotes(projectId);
    payload = {
      notes: response.notes.filter((note) => matchesSearch(query, note.title, note.description)),
    };
  } else if (domain === "notes" && action === "create") {
    payload = await createNote(Number(rest[0]), { title: rest.slice(1).join(" ").trim() || undefined });
  } else if (domain === "notes" && action === "get") {
    payload = await getNote(Number(rest[0]));
  } else if (domain === "notes" && action === "update") {
    const noteId = Number(rest[0]);
    const next = {};
    for (let index = 1; index < rest.length; index += 2) {
      const flag = rest[index];
      const value = rest[index + 1] ?? "";
      if (flag === "--title") {
        next.title = value;
      } else if (flag === "--description") {
        next.description = value;
      }
    }
    payload = await updateNote(noteId, next);
  } else if (domain === "notes" && action === "delete") {
    payload = await deleteNote(Number(rest[0]));
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
