#!/usr/bin/env node

import fs from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { _electron as electron } from "playwright";

function parseArgs(argv) {
  const options = {
    electronExecutable: "",
    electronArg: [],
    actions: "",
    outDir: "",
    startupTimeoutMs: 120000,
    stepTimeoutMs: 15000,
    initialWaitMs: 1200,
    windowIndex: 0,
    cwd: process.cwd(),
    envJson: "",
  };

  for (let index = 0; index < argv.length; index += 1) {
    const token = argv[index];
    const next = argv[index + 1];
    switch (token) {
      case "--electron-executable":
        options.electronExecutable = next;
        index += 1;
        break;
      case "--electron-arg":
        options.electronArg.push(next);
        index += 1;
        break;
      case "--actions":
        options.actions = next;
        index += 1;
        break;
      case "--out-dir":
        options.outDir = next;
        index += 1;
        break;
      case "--startup-timeout-ms":
        options.startupTimeoutMs = Number.parseInt(next, 10);
        index += 1;
        break;
      case "--step-timeout-ms":
        options.stepTimeoutMs = Number.parseInt(next, 10);
        index += 1;
        break;
      case "--initial-wait-ms":
        options.initialWaitMs = Number.parseInt(next, 10);
        index += 1;
        break;
      case "--window-index":
        options.windowIndex = Number.parseInt(next, 10);
        index += 1;
        break;
      case "--cwd":
        options.cwd = next;
        index += 1;
        break;
      case "--env-json":
        options.envJson = next;
        index += 1;
        break;
      case "--help":
      case "-h":
        printHelp();
        process.exit(0);
      default:
        throw new Error(`unknown arg: ${token}`);
    }
  }

  if (!options.electronExecutable) {
    throw new Error("--electron-executable is required");
  }
  if (!options.outDir) {
    throw new Error("--out-dir is required");
  }
  return options;
}

function printHelp() {
  console.log(`Usage: electron_runner.mjs [options]

Options:
  --electron-executable <path>   required
  --electron-arg <value>         repeatable
  --actions <json_path>          actions file path
  --out-dir <path>               required
  --cwd <path>                   process cwd for the launched app
  --env-json <json>              JSON object of environment variables
  --startup-timeout-ms <int>     default: 120000
  --step-timeout-ms <int>        default: 15000
  --initial-wait-ms <int>        default: 1200
  --window-index <int>           default: 0
`);
}

function timestampSlug() {
  const now = new Date();
  const pad = (value) => `${value}`.padStart(2, "0");
  return (
    `${now.getFullYear()}` +
    `${pad(now.getMonth() + 1)}` +
    `${pad(now.getDate())}-` +
    `${pad(now.getHours())}` +
    `${pad(now.getMinutes())}` +
    `${pad(now.getSeconds())}`
  );
}

function resolvePathMaybeRelative(candidate, baseDir) {
  if (path.isAbsolute(candidate)) {
    return path.resolve(candidate);
  }
  return path.resolve(baseDir, candidate);
}

function resolveMouseButton(value) {
  if (value === undefined) {
    return "left";
  }
  const button = String(value);
  if (!["left", "right", "middle"].includes(button)) {
    throw new Error(`unsupported mouse button: ${button}`);
  }
  return button;
}

async function loadActions(actionsPath) {
  if (!actionsPath) {
    return [];
  }
  const raw = await fs.readFile(actionsPath, "utf-8");
  const payload = JSON.parse(raw);
  const actions = Array.isArray(payload) ? payload : payload.actions ?? [];
  if (!Array.isArray(actions)) {
    throw new Error("actions must be array or {actions:[...]}");
  }
  for (let index = 0; index < actions.length; index += 1) {
    const action = actions[index];
    if (typeof action !== "object" || action === null || !("type" in action)) {
      throw new Error(`invalid action at index ${index}`);
    }
  }
  return actions;
}

function defaultScreenshotPath(outDir) {
  return path.join(outDir, `${timestampSlug()}-window.png`);
}

async function runAction({ page, action, outDir, timeoutMs }) {
  const type = String(action.type);
  const captures = [];

  if (type === "wait_for_timeout") {
    await page.waitForTimeout(Number(action.ms ?? 0));
    return captures;
  }

  if (type === "wait_for_selector") {
    await page.waitForSelector(String(action.selector), {
      state: String(action.state ?? "visible"),
      timeout: timeoutMs,
    });
    return captures;
  }

  if (type === "assert_computed_style") {
    const selector = String(action.selector);
    const property = String(action.property);
    const expected = String(action.equals);
    const locator = page.locator(selector).first();
    await locator.waitFor({ state: "visible", timeout: timeoutMs });
    const actual = await locator.evaluate((element, styleProperty) => getComputedStyle(element).getPropertyValue(styleProperty), property);
    if (actual.trim() !== expected) {
      throw new Error(`assert_computed_style failed for ${selector}: expected ${property}=${expected}, got ${actual.trim()}`);
    }
    return captures;
  }

  if (type === "assert_value") {
    const selector = String(action.selector);
    const expected = String(action.equals ?? "");
    const locator = page.locator(selector).first();
    await locator.waitFor({ state: "visible", timeout: timeoutMs });
    const actual = await locator.inputValue();
    if (actual !== expected) {
      throw new Error(`assert_value failed for ${selector}: expected ${expected}, got ${actual}`);
    }
    return captures;
  }

  if (type === "assert_below") {
    const upperSelector = String(action.upper_selector);
    const lowerSelector = String(action.lower_selector);
    const upper = page.locator(upperSelector).first();
    const lower = page.locator(lowerSelector).first();
    await upper.waitFor({ state: "visible", timeout: timeoutMs });
    await lower.waitFor({ state: "visible", timeout: timeoutMs });
    const upperBox = await upper.boundingBox();
    const lowerBox = await lower.boundingBox();
    if (!upperBox || !lowerBox) {
      throw new Error(`assert_below failed: unable to read bounding boxes for ${upperSelector} or ${lowerSelector}`);
    }
    if (lowerBox.y < upperBox.y + upperBox.height) {
      throw new Error(`assert_below failed: ${lowerSelector} overlaps ${upperSelector}`);
    }
    return captures;
  }

  if (type === "log_bounding_box") {
    const selector = String(action.selector);
    const locator = page.locator(selector).first();
    await locator.waitFor({ state: "visible", timeout: timeoutMs });
    const box = await locator.boundingBox();
    console.error(`[bounding-box] ${selector} ${JSON.stringify(box)}`);
    return captures;
  }

  if (type === "log_selector_count") {
    const selector = String(action.selector);
    const count = await page.locator(selector).count();
    console.error(`[selector-count] ${selector} ${count}`);
    return captures;
  }

  if (type === "wait_for_enabled") {
    const selector = String(action.selector);
    const locator = page.locator(selector).first();
    await locator.waitFor({ state: "visible", timeout: timeoutMs });
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      if (await locator.isEnabled()) {
        return captures;
      }
      await page.waitForTimeout(100);
    }
    throw new Error(`wait_for_enabled timed out for selector: ${selector}`);
  }

  if (type === "set_viewport_size") {
    await page.setViewportSize({
      width: Number(action.width),
      height: Number(action.height),
    });
    return captures;
  }

  if (type === "click") {
    await page.click(String(action.selector), {
      timeout: timeoutMs,
      button: resolveMouseButton(action.button),
    });
    return captures;
  }

  if (type === "hover") {
    await page.hover(String(action.selector), { timeout: timeoutMs });
    return captures;
  }

  if (type === "fill") {
    await page.fill(String(action.selector), String(action.value ?? ""), { timeout: timeoutMs });
    return captures;
  }

  if (type === "select_option") {
    const selector = String(action.selector);
    const optionSpec =
      action.value !== undefined
        ? String(action.value)
        : action.label !== undefined
          ? { label: String(action.label) }
          : action.index !== undefined
            ? { index: Number(action.index) }
            : null;
    if (optionSpec === null) {
      throw new Error("select_option requires one of: value, label, index");
    }
    await page.selectOption(selector, optionSpec, { timeout: timeoutMs });
    return captures;
  }

  if (type === "press") {
    await page.press(String(action.selector), String(action.key), { timeout: timeoutMs });
    return captures;
  }

  if (type === "keyboard_press") {
    await page.keyboard.press(String(action.key), { delay: Number(action.delay_ms ?? 0) });
    return captures;
  }

  if (type === "type") {
    await page.type(String(action.selector), String(action.value ?? ""), {
      delay: Number(action.delay_ms ?? 0),
      timeout: timeoutMs,
    });
    return captures;
  }

  if (type === "keyboard_type") {
    await page.keyboard.type(String(action.value ?? ""), { delay: Number(action.delay_ms ?? 0) });
    return captures;
  }

  if (type === "drag") {
    const selector = String(action.selector);
    const locator = page.locator(selector).first();
    await locator.waitFor({ state: "visible", timeout: timeoutMs });
    const box = await locator.boundingBox();
    if (!box) {
      throw new Error(`unable to resolve drag target box for selector: ${selector}`);
    }
    const startX = box.x + box.width / 2;
    const startY = box.y + box.height / 2;
    await page.mouse.move(startX, startY);
    await page.mouse.down({ button: resolveMouseButton(action.button) });
    await page.mouse.move(
      startX + Number(action.delta_x ?? 0),
      startY + Number(action.delta_y ?? 0),
      { steps: Number(action.steps ?? 14) },
    );
    await page.mouse.up({ button: resolveMouseButton(action.button) });
    return captures;
  }

  if (type === "mouse_down") {
    const selector = String(action.selector);
    const locator = page.locator(selector).first();
    await locator.waitFor({ state: "visible", timeout: timeoutMs });
    const box = await locator.boundingBox();
    if (!box) {
      throw new Error(`unable to resolve mouse_down target box for selector: ${selector}`);
    }
    const offsetX = action.offset_x !== undefined ? Number(action.offset_x) : box.width / 2;
    const offsetY = action.offset_y !== undefined ? Number(action.offset_y) : box.height / 2;
    await page.mouse.move(box.x + offsetX, box.y + offsetY);
    await page.mouse.down({ button: resolveMouseButton(action.button) });
    return captures;
  }

  if (type === "mouse_move") {
    if (action.selector !== undefined) {
      const selector = String(action.selector);
      const locator = page.locator(selector).first();
      await locator.waitFor({ state: "visible", timeout: timeoutMs });
      const box = await locator.boundingBox();
      if (!box) {
        throw new Error(`unable to resolve mouse_move target box for selector: ${selector}`);
      }
      const offsetX = action.offset_x !== undefined ? Number(action.offset_x) : box.width / 2;
      const offsetY = action.offset_y !== undefined ? Number(action.offset_y) : box.height / 2;
      await page.mouse.move(box.x + offsetX, box.y + offsetY, { steps: Number(action.steps ?? 12) });
      return captures;
    }
    await page.mouse.move(Number(action.x), Number(action.y), { steps: Number(action.steps ?? 12) });
    return captures;
  }

  if (type === "mouse_up") {
    await page.mouse.up({ button: resolveMouseButton(action.button) });
    return captures;
  }

  if (type === "scroll") {
    const selector = String(action.selector);
    const locator = page.locator(selector).first();
    await locator.waitFor({ state: "visible", timeout: timeoutMs });
    await locator.evaluate(
      (element, delta) => {
        element.scrollBy({ left: delta.x, top: delta.y, behavior: "auto" });
      },
      { x: Number(action.delta_x ?? 0), y: Number(action.delta_y ?? 0) },
    );
    return captures;
  }

  if (type === "screenshot") {
    const requestedPath = action.path ? String(action.path) : defaultScreenshotPath(outDir);
    const resolvedPath = resolvePathMaybeRelative(requestedPath, outDir);
    await fs.mkdir(path.dirname(resolvedPath), { recursive: true });
    await page.screenshot({
      path: resolvedPath,
      fullPage: Boolean(action.full_page ?? false),
    });
    captures.push(resolvedPath);
    return captures;
  }

  throw new Error(`unsupported action type: ${type}`);
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  const cwd = resolvePathMaybeRelative(options.cwd, process.cwd());
  const outDir = resolvePathMaybeRelative(options.outDir, process.cwd());
  const executablePath = resolvePathMaybeRelative(options.electronExecutable, cwd);
  const actionsPath = options.actions ? resolvePathMaybeRelative(options.actions, process.cwd()) : "";
  const env = options.envJson ? JSON.parse(options.envJson) : {};

  await fs.mkdir(outDir, { recursive: true });
  const actions = await loadActions(actionsPath);

  const app = await electron.launch({
    executablePath,
    args: options.electronArg,
    cwd,
    chromiumSandbox: false,
    env: { ...process.env, ...env },
  });

  const processOutput = [];
  const childProcess = app.process();
  const attachStream = (stream, label) => {
    if (!stream) {
      return;
    }
    stream.on("data", (chunk) => {
      const text = String(chunk);
      processOutput.push(`[${label}] ${text}`);
      process.stderr.write(text);
    });
  };
  attachStream(childProcess?.stdout, "stdout");
  attachStream(childProcess?.stderr, "stderr");

  const captures = [];
  try {
    let firstWindow;
    try {
      firstWindow = await app.firstWindow({ timeout: options.startupTimeoutMs });
    } catch (error) {
      if (processOutput.length > 0) {
        process.stderr.write(`\n[startup-output]\n${processOutput.join("")}\n`);
      }
      throw error;
    }
    const windows = app.windows();
    const page = windows[options.windowIndex] ?? firstWindow;
    if (!page) {
      throw new Error("no electron windows available");
    }

    page.on("console", (message) => {
      process.stderr.write(`[renderer-console:${message.type()}] ${message.text()}\n`);
    });
    page.on("pageerror", (error) => {
      process.stderr.write(`[renderer-error] ${error?.stack ?? String(error)}\n`);
    });

    await page.waitForTimeout(options.initialWaitMs);
    if (actions.length === 0) {
      const outputPath = defaultScreenshotPath(outDir);
      await page.screenshot({ path: outputPath, fullPage: false });
      captures.push(path.resolve(outputPath));
    } else {
      for (const action of actions) {
        const actionCaptures = await runAction({
          page,
          action,
          outDir,
          timeoutMs: options.stepTimeoutMs,
        });
        captures.push(...actionCaptures);
      }
    }
  } finally {
    try {
      await Promise.race([
        app.close(),
        new Promise((_, reject) => {
          setTimeout(() => reject(new Error("electron close timeout")), 1500);
        }),
      ]);
    } catch (_error) {
      try {
        await app.evaluate(async ({ app: electronApp }) => {
          electronApp.quit();
        });
      } catch (_quitError) {
        // ignore and fall through to process kill
      }
      childProcess?.kill("SIGKILL");
    }
  }

  for (const capture of captures) {
    console.log(capture);
  }
}

main().catch((error) => {
  console.error(error?.stack ?? String(error));
  process.exit(1);
});
