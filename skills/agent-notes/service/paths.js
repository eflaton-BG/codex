import os from "node:os";
import path from "node:path";
import crypto from "node:crypto";

export function resolveStateDir() {
  if (process.env.AGENT_NOTES_STATE_DIR) {
    return path.resolve(process.env.AGENT_NOTES_STATE_DIR);
  }
  const baseDir = process.env.XDG_STATE_HOME
    ? path.resolve(process.env.XDG_STATE_HOME)
    : path.join(os.homedir(), ".local", "state");
  return path.join(baseDir, "agent_notes");
}

export function resolvePaths() {
  const stateDir = resolveStateDir();
  const defaultSocketPath = path.join(stateDir, "agent-notes.sock");
  const socketPath = process.env.AGENT_NOTES_SOCKET_PATH
    ? path.resolve(process.env.AGENT_NOTES_SOCKET_PATH)
    : defaultSocketPath.length <= 96
      ? defaultSocketPath
      : path.join(
          "/tmp",
          `agent-notes-${crypto.createHash("sha1").update(stateDir).digest("hex").slice(0, 12)}.sock`,
        );
  return {
    stateDir,
    socketPath,
    dbPath: path.join(stateDir, "agent-notes.sqlite"),
    logPath: path.join(stateDir, "agent-notes.log"),
  };
}
