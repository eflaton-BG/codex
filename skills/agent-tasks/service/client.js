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
    request.on("error", reject);
    if (body) {
      request.write(body);
    }
    request.end();
  });
}

function buildQueryString(params = {}) {
  const searchParams = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === false || value === "") {
      continue;
    }
    searchParams.set(key, String(value));
  }
  const query = searchParams.toString();
  return query ? `?${query}` : "";
}

export async function pingService() {
  return await requestJson("GET", "/health");
}

export async function fetchTree(params = {}) {
  return await requestJson("GET", `/tree${buildQueryString(params)}`);
}

export async function fetchTodo() {
  return await requestJson("GET", "/todo");
}

export async function fetchNode(nodeId) {
  return await requestJson("GET", `/nodes/${nodeId}`);
}

export async function searchNodes(params = {}) {
  return await requestJson("GET", `/search${buildQueryString(params)}`);
}

export async function createNode(payload) {
  return await requestJson("POST", "/nodes", JSON.stringify(payload));
}

export async function updateNode(nodeId, payload) {
  return await requestJson("PATCH", `/nodes/${nodeId}`, JSON.stringify(payload));
}

export async function moveNode(nodeId, payload) {
  return await requestJson("POST", `/nodes/${nodeId}/move`, JSON.stringify(payload));
}

export async function moveTodoNode(nodeId, payload) {
  return await requestJson("POST", `/todo/${nodeId}/move`, JSON.stringify(payload));
}

export async function deleteNode(nodeId) {
  return await requestJson("DELETE", `/nodes/${nodeId}`);
}

export async function addStep(nodeId, payload = {}) {
  return await requestJson("POST", `/nodes/${nodeId}/steps`, JSON.stringify(payload));
}

export async function updateStep(stepId, payload) {
  return await requestJson("PATCH", `/steps/${stepId}`, JSON.stringify(payload));
}

export async function deleteStep(stepId) {
  return await requestJson("DELETE", `/steps/${stepId}`);
}

export async function addComment(nodeId, payload = {}) {
  return await requestJson("POST", `/nodes/${nodeId}/comments`, JSON.stringify(payload));
}

export async function updateComment(commentId, payload) {
  return await requestJson("PATCH", `/comments/${commentId}`, JSON.stringify(payload));
}

export async function deleteComment(commentId) {
  return await requestJson("DELETE", `/comments/${commentId}`);
}

export function subscribeEvents({ onEvent, onError, onOpen }) {
  const { socketPath } = resolvePaths();
  let closedByCaller = false;
  const request = http.request(
    {
      method: "GET",
      path: "/events",
      socketPath,
      headers: { accept: "text/event-stream" },
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
          const eventName = lines.filter((line) => line.startsWith("event:")).map((line) => line.slice(6).trim())[0] ?? "message";
          const dataLines = lines.filter((line) => line.startsWith("data:")).map((line) => line.slice(5).trimStart());
          if (dataLines.length === 0) {
            continue;
          }
          try {
            onEvent?.({ event: eventName, payload: JSON.parse(dataLines.join("\n")) });
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
