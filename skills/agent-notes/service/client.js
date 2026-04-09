import http from "node:http";
import { resolvePaths } from "./paths.js";

async function requestJson(method, pathname, body) {
  const { socketPath } = resolvePaths();
  return await new Promise((resolve, reject) => {
    const request = http.request(
      {
        method,
        path: pathname,
        socketPath,
        headers: body
          ? {
              "content-type": "application/json",
              "content-length": Buffer.byteLength(body),
            }
          : undefined,
      },
      (response) => {
        const chunks = [];
        response.on("data", (chunk) => chunks.push(Buffer.from(chunk)));
        response.on("end", () => {
          const text = Buffer.concat(chunks).toString("utf-8");
          const payload = text ? JSON.parse(text) : null;
          if ((response.statusCode ?? 500) >= 400) {
            reject(new Error(payload?.error || `request failed: ${response.statusCode}`));
            return;
          }
          resolve(payload);
        });
      },
    );

    request.on("error", (error) => reject(error));
    if (body) {
      request.write(body);
    }
    request.end();
  });
}

export async function pingService() {
  return await requestJson("GET", "/health");
}

export async function listProjects() {
  return await requestJson("GET", "/projects");
}

export async function createProject(payload = {}) {
  return await requestJson("POST", "/projects", JSON.stringify(payload));
}

export async function updateProject(projectId, payload) {
  return await requestJson("PATCH", `/projects/${projectId}`, JSON.stringify(payload));
}

export async function deleteProject(projectId) {
  return await requestJson("DELETE", `/projects/${projectId}`);
}

export async function listNotes(projectId) {
  return await requestJson("GET", `/projects/${projectId}/notes`);
}

export async function createNote(projectId, payload = {}) {
  return await requestJson("POST", `/projects/${projectId}/notes`, JSON.stringify(payload));
}

export async function getNote(noteId) {
  return await requestJson("GET", `/notes/${noteId}`);
}

export async function updateNote(noteId, payload) {
  return await requestJson("PATCH", `/notes/${noteId}`, JSON.stringify(payload));
}

export async function deleteNote(noteId) {
  return await requestJson("DELETE", `/notes/${noteId}`);
}

export function subscribeEvents({ onEvent, onError, onOpen }) {
  const { socketPath } = resolvePaths();
  let closedByCaller = false;
  const request = http.request(
    {
      method: "GET",
      path: "/events",
      socketPath,
      headers: {
        accept: "text/event-stream",
      },
    },
    (response) => {
      if ((response.statusCode ?? 500) >= 400) {
        onError?.(new Error(`event subscription failed: ${response.statusCode}`));
        return;
      }

      onOpen?.();
      response.setEncoding("utf-8");

      let buffer = "";
      response.on("data", (chunk) => {
        buffer += chunk;
        const segments = buffer.split(/\r?\n\r?\n/);
        buffer = segments.pop() ?? "";
        for (const segment of segments) {
          const lines = segment.split(/\r?\n/);
          const eventName = lines
            .filter((line) => line.startsWith("event:"))
            .map((line) => line.slice(6).trim())[0] ?? "message";
          const dataLines = lines
            .filter((line) => line.startsWith("data:"))
            .map((line) => line.slice(5).trimStart());
          if (dataLines.length === 0) {
            continue;
          }
          try {
            const payload = JSON.parse(dataLines.join("\n"));
            onEvent?.({ event: eventName, payload });
          } catch (error) {
            onError?.(error);
          }
        }
      });

      response.on("error", (error) => {
        if (!closedByCaller) {
          onError?.(error);
        }
      });
      response.on("end", () => {
        if (!closedByCaller) {
          onError?.(new Error("event subscription ended"));
        }
      });
      response.on("close", () => {
        if (!closedByCaller) {
          onError?.(new Error("event subscription closed"));
        }
      });
    },
  );

  request.on("error", (error) => {
    if (!closedByCaller) {
      onError?.(error);
    }
  });
  request.end();

  return () => {
    closedByCaller = true;
    request.destroy();
  };
}
