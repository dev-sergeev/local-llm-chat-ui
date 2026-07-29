import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { access, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import test from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const CSS_PATH = path.join(ROOT, "src/datalab_chat/static/assets/app.css");
const DEVTOOLS_CONNECT_TIMEOUT_MS = 5_000;
const DEVTOOLS_COMMAND_TIMEOUT_MS = 10_000;

async function findChromium() {
  const names = process.platform === "win32"
    ? ["chrome.exe", "chromium.exe"]
    : ["chromium", "chromium-browser", "google-chrome", "google-chrome-stable"];
  const pathCandidates = (process.env.PATH || "")
    .split(path.delimiter)
    .flatMap((directory) => names.map((name) => path.join(directory, name)));
  const candidates = [
    process.env.DATALAB_CHROMIUM,
    ...pathCandidates,
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
  ].filter(Boolean);

  for (const candidate of candidates) {
    try {
      await access(candidate);
      return candidate;
    } catch {
      // Try the next known executable.
    }
  }
  return null;
}

async function waitForDevToolsPort(profileDirectory, browserProcess) {
  const activePortFile = path.join(profileDirectory, "DevToolsActivePort");
  for (let attempt = 0; attempt < 150; attempt += 1) {
    if (browserProcess.exitCode !== null || browserProcess.signalCode !== null) {
      const reason = browserProcess.exitCode ?? browserProcess.signalCode;
      throw new Error(`Chromium exited before DevTools was ready (${reason})`);
    }
    try {
      const [port] = (await readFile(activePortFile, "utf8")).trim().split("\n");
      return Number(port);
    } catch {
      await delay(100);
    }
  }
  throw new Error("Timed out waiting for Chromium DevTools port");
}

async function waitForPageTarget(port, fixtureUrl) {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    try {
      const response = await fetch(`http://127.0.0.1:${port}/json/list`);
      const targets = await response.json();
      const target = targets.find(
        (candidate) => candidate.type === "page" && candidate.url.startsWith(fixtureUrl),
      ) || targets.find((candidate) => candidate.type === "page");
      if (target?.webSocketDebuggerUrl) return target;
    } catch {
      // Chromium can expose the port just before the page target exists.
    }
    await delay(100);
  }
  throw new Error("Timed out waiting for Chromium page target");
}

async function stopBrowser(browserProcess) {
  if (browserProcess.exitCode !== null || browserProcess.signalCode !== null) return;
  await new Promise((resolve) => {
    const finish = () => {
      clearTimeout(timeout);
      browserProcess.removeListener("exit", finish);
      browserProcess.removeListener("error", finish);
      resolve();
    };
    const timeout = setTimeout(finish, 5_000);
    browserProcess.once("exit", finish);
    browserProcess.once("error", finish);
    browserProcess.kill("SIGKILL");
  });
}

class DevToolsClient {
  constructor(webSocket) {
    this.webSocket = webSocket;
    this.nextId = 1;
    this.pending = new Map();
    webSocket.addEventListener("message", (event) => {
      const message = JSON.parse(event.data);
      if (!message.id) return;
      const pending = this.pending.get(message.id);
      if (!pending) return;
      this.pending.delete(message.id);
      clearTimeout(pending.timeout);
      if (message.error) pending.reject(new Error(message.error.message));
      else pending.resolve(message.result);
    });
    webSocket.addEventListener("close", () => {
      this.rejectPending(new Error("Chromium DevTools connection closed"));
    });
    webSocket.addEventListener("error", () => {
      this.rejectPending(new Error("Chromium DevTools connection failed"));
    });
  }

  static async connect(url) {
    const webSocket = new WebSocket(url);
    await new Promise((resolve, reject) => {
      const cleanup = () => {
        clearTimeout(timeout);
        webSocket.removeEventListener("open", handleOpen);
        webSocket.removeEventListener("error", handleError);
      };
      const handleOpen = () => {
        cleanup();
        resolve();
      };
      const handleError = () => {
        cleanup();
        reject(new Error("Could not connect to Chromium DevTools"));
      };
      const timeout = setTimeout(() => {
        cleanup();
        webSocket.close();
        reject(new Error("Timed out connecting to Chromium DevTools"));
      }, DEVTOOLS_CONNECT_TIMEOUT_MS);
      webSocket.addEventListener("open", handleOpen, { once: true });
      webSocket.addEventListener("error", handleError, { once: true });
    });
    return new DevToolsClient(webSocket);
  }

  send(method, params = {}) {
    const id = this.nextId;
    this.nextId += 1;
    return new Promise((resolve, reject) => {
      const timeout = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`Timed out waiting for Chromium DevTools command ${method}`));
      }, DEVTOOLS_COMMAND_TIMEOUT_MS);
      this.pending.set(id, { resolve, reject, timeout });
      try {
        this.webSocket.send(JSON.stringify({ id, method, params }));
      } catch (error) {
        clearTimeout(timeout);
        this.pending.delete(id);
        reject(error);
      }
    });
  }

  rejectPending(error) {
    for (const pending of this.pending.values()) {
      clearTimeout(pending.timeout);
      pending.reject(error);
    }
    this.pending.clear();
  }

  close() {
    this.webSocket.close();
  }
}

async function measure(client, width, height) {
  await client.send("Emulation.setDeviceMetricsOverride", {
    width,
    height,
    deviceScaleFactor: 1,
    mobile: width <= 820,
  });
  const result = await client.send("Runtime.evaluate", {
    expression: `(() => {
      const messages = document.querySelector("#messages");
      const composer = document.querySelector("#composer-wrap");
      const mainPanel = document.querySelector("#main-panel");
      const textarea = composer.querySelector("textarea");
      const lastParagraph = messages.querySelector(".message-content p:last-child");
      textarea.style.height = "180px";
      messages.style.scrollBehavior = "auto";
      messages.scrollTop = messages.scrollHeight;
      return {
        viewportHeight: window.innerHeight,
        documentHeight: document.documentElement.scrollHeight,
        messagesClientHeight: messages.clientHeight,
        messagesScrollHeight: messages.scrollHeight,
        messagesBottom: messages.getBoundingClientRect().bottom,
        composerTop: composer.getBoundingClientRect().top,
        composerBottom: composer.getBoundingClientRect().bottom,
        mainPanelBottom: mainPanel.getBoundingClientRect().bottom,
        lastParagraphBottom: lastParagraph.getBoundingClientRect().bottom,
        textareaHeight: textarea.getBoundingClientRect().height,
      };
    })()`,
    returnByValue: true,
  });
  return result.result.value;
}

test("a long chat scrolls inside the viewport and keeps the composer visible", async (context) => {
  if (typeof WebSocket !== "function" || typeof fetch !== "function") {
    context.skip("This optional browser check needs Node.js with fetch and WebSocket support");
    return;
  }
  const chromium = await findChromium();
  if (!chromium) {
    context.skip("Chromium is not installed; the static CSS contract remains active");
    return;
  }

  const temporaryDirectory = await mkdtemp(path.join(os.tmpdir(), "datalab-layout-"));
  const profileDirectory = path.join(temporaryDirectory, "profile");
  const fixturePath = path.join(temporaryDirectory, "long-chat.html");
  const css = await readFile(CSS_PATH, "utf8");
  const paragraphs = Array.from(
    { length: 48 },
    (_, index) => `<p>Строка длинного ответа ${index}: проверка прокрутки области сообщений.</p>`,
  ).join("\n");
  const document = `<!doctype html>
<html lang="ru">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>${css}</style>
  </head>
  <body>
    <div class="app-shell">
      <aside class="sidebar">История диалогов</aside>
      <main class="main-panel" id="main-panel">
        <header class="topbar">DataLab Risk Chat</header>
        <section class="chat-view">
          <div class="messages" id="messages">
            <div class="messages-inner">
              <article class="message assistant">
                <div class="message-avatar">DR</div>
                <div class="message-card">
                  <div class="message-content">${paragraphs}</div>
                </div>
              </article>
            </div>
          </div>
          <button class="jump-bottom" type="button" hidden>К последнему сообщению</button>
          <div class="composer-wrap" id="composer-wrap">
            <form class="composer">
              <textarea rows="1">Следующий вопрос</textarea>
              <div class="composer-footer">
                <span class="composer-hint">Enter — отправить</span>
                <div class="composer-buttons"><button class="send-button" type="button">↑</button></div>
              </div>
            </form>
            <p class="disclaimer">Проверяйте значимые выводы.</p>
          </div>
        </section>
      </main>
    </div>
  </body>
</html>`;
  await writeFile(fixturePath, document, "utf8");
  const fixtureUrl = pathToFileURL(fixturePath).href;
  const browserProcess = spawn(
    chromium,
    [
      "--headless=new",
      "--disable-background-networking",
      "--disable-component-update",
      "--disable-default-apps",
      "--disable-dev-shm-usage",
      "--disable-extensions",
      "--disable-gpu",
      "--disable-sync",
      "--metrics-recording-only",
      "--no-default-browser-check",
      "--no-first-run",
      "--no-sandbox",
      "--remote-allow-origins=*",
      "--remote-debugging-port=0",
      `--user-data-dir=${profileDirectory}`,
      fixtureUrl,
    ],
    { stdio: "ignore" },
  );

  let client;
  try {
    const port = await waitForDevToolsPort(profileDirectory, browserProcess);
    const target = await waitForPageTarget(port, fixtureUrl);
    client = await DevToolsClient.connect(target.webSocketDebuggerUrl);
    const desktop = await measure(client, 1280, 800);
    const mobile = await measure(client, 390, 844);

    for (const [viewport, metrics] of Object.entries({ desktop, mobile })) {
      assert.ok(
        metrics.documentHeight <= metrics.viewportHeight + 1,
        `${viewport}: document escaped viewport: ${JSON.stringify(metrics)}`,
      );
      assert.ok(
        metrics.messagesScrollHeight > metrics.messagesClientHeight + 100,
        `${viewport}: messages did not become scrollable: ${JSON.stringify(metrics)}`,
      );
      assert.ok(
        metrics.textareaHeight >= 179,
        `${viewport}: composer did not reach its maximum height: ${JSON.stringify(metrics)}`,
      );
      assert.ok(
        metrics.messagesBottom <= metrics.composerTop + 1,
        `${viewport}: expanded composer overlaps the message viewport: ${JSON.stringify(metrics)}`,
      );
      assert.ok(
        metrics.lastParagraphBottom <= metrics.composerTop + 1,
        `${viewport}: final response content is hidden by the composer: ${JSON.stringify(metrics)}`,
      );
      assert.ok(
        metrics.composerBottom <= metrics.viewportHeight + 1,
        `${viewport}: composer is below viewport: ${JSON.stringify(metrics)}`,
      );
      assert.ok(
        metrics.mainPanelBottom <= metrics.viewportHeight + 1,
        `${viewport}: main panel is taller than viewport: ${JSON.stringify(metrics)}`,
      );
    }
  } finally {
    client?.close();
    await stopBrowser(browserProcess);
    await rm(temporaryDirectory, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
  }
});
