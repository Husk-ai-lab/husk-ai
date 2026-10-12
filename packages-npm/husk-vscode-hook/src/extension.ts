// Husk for VS Code (and Antigravity, which is a VS Code fork).
//
// Streams every terminal command the IDE runs — including commands that
// AI agents (Copilot, Cline, Continue, Roo, Antigravity's native agent)
// kick off via the Terminal Shell Integration API — into the local Husk
// Studio at http://localhost:7654. The user sees every action their agent
// took, with arguments and cwd, in the Studio's activity feed.
//
// Honest scope of v0.1:
//  - Observability only. Events flow into Husk fire-and-forget; the IDE
//    is never blocked and never prompted for a decision.
//
// Future:
//  - Group captured commands by run / agent thread so the Studio can show
//    them as nested spans alongside LangGraph and OTel traces.

import * as vscode from "vscode";

const STATUS_PRIORITY = 100;
const RECONNECT_MS = 5_000;

interface HuskEventBody {
  hook: string;
  payload: Record<string, unknown>;
  project?: string;
}

type ConnectionState = "online" | "offline" | "capturing";

function huskUrl(): string {
  return (
    vscode.workspace.getConfiguration("husk").get<string>("url") ||
    "http://localhost:7654"
  ).replace(/\/$/, "");
}

function captureEnabled(): boolean {
  return (
    vscode.workspace.getConfiguration("husk").get<boolean>("captureTerminal") ??
    true
  );
}

async function ping(url: string): Promise<boolean> {
  try {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), 1500);
    try {
      const r = await fetch(`${url}/api/health`, { signal: ctrl.signal });
      return r.ok;
    } finally {
      clearTimeout(t);
    }
  } catch {
    return false;
  }
}

async function postEvent(url: string, body: HuskEventBody): Promise<void> {
  try {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), 2000);
    try {
      await fetch(`${url}/api/cursor/events`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
        signal: ctrl.signal,
      });
    } finally {
      clearTimeout(t);
    }
  } catch {
    // Husk not reachable — drop silently. The status bar already shows offline.
  }
}

function projectFromWorkspace(): string | undefined {
  const folders = vscode.workspace.workspaceFolders;
  if (!folders || folders.length === 0) return undefined;
  return folders[0].name;
}

class HuskBridge {
  private status: vscode.StatusBarItem;
  private state: ConnectionState = "offline";
  private heartbeat: NodeJS.Timeout | undefined;
  private disposables: vscode.Disposable[] = [];

  constructor(private readonly ctx: vscode.ExtensionContext) {
    this.status = vscode.window.createStatusBarItem(
      vscode.StatusBarAlignment.Right,
      STATUS_PRIORITY,
    );
    this.status.command = "husk.openStudio";
    this.disposables.push(this.status);
    this.render();
    this.status.show();
  }

  async start(): Promise<void> {
    await this.checkConnection();
    this.heartbeat = setInterval(() => {
      void this.checkConnection();
    }, RECONNECT_MS);
    this.attachTerminalListener();
  }

  dispose(): void {
    if (this.heartbeat) clearInterval(this.heartbeat);
    for (const d of this.disposables) d.dispose();
  }

  async checkConnection(): Promise<void> {
    const ok = await ping(huskUrl());
    const next: ConnectionState = ok
      ? captureEnabled()
        ? "capturing"
        : "online"
      : "offline";
    if (next !== this.state) {
      this.state = next;
      this.render();
    }
  }

  private render(): void {
    switch (this.state) {
      case "capturing":
        this.status.text = "$(record) Husk";
        this.status.tooltip = `Husk capturing terminal commands → ${huskUrl()}\nClick to open the Studio.`;
        this.status.backgroundColor = undefined;
        break;
      case "online":
        this.status.text = "$(eye-closed) Husk";
        this.status.tooltip = `Husk connected (capture off) → ${huskUrl()}\nClick to open the Studio.`;
        this.status.backgroundColor = undefined;
        break;
      case "offline":
        this.status.text = "$(plug) Husk · offline";
        this.status.tooltip = `Husk not reachable at ${huskUrl()}.\nStart it with: husk-studio start`;
        this.status.backgroundColor = new vscode.ThemeColor(
          "statusBarItem.warningBackground",
        );
        break;
    }
  }

  private attachTerminalListener(): void {
    // VS Code 1.85+ exposes Terminal Shell Integration. When an agent (or the
    // user) runs a command in any terminal, this fires with the command line.
    const anyWindow = vscode.window as unknown as {
      onDidStartTerminalShellExecution?: vscode.Event<{
        terminal: vscode.Terminal;
        shellIntegration: unknown;
        execution: { commandLine: { value: string; isTrusted: boolean } };
      }>;
    };
    const fn = anyWindow.onDidStartTerminalShellExecution;
    if (typeof fn !== "function") {
      // Older host without shell integration — log once and continue with
      // observability of opens only.
      console.warn(
        "husk-vscode-hook: terminal shell integration not available; commands will not be captured.",
      );
      return;
    }
    const sub = fn((evt) => {
      if (!captureEnabled() || this.state === "offline") return;
      const cmd = evt.execution?.commandLine?.value || "";
      if (!cmd.trim()) return;
      const ide = isAntigravity()
        ? "antigravity"
        : isCursor()
          ? "cursor-ide"
          : "vscode";
      void postEvent(huskUrl(), {
        hook: `${ide}.shell.execution`,
        payload: {
          command: cmd,
          cwd: evt.terminal.shellIntegration?.cwd
            ? String(evt.terminal.shellIntegration.cwd)
            : undefined,
          terminal_name: evt.terminal.name,
          ide,
        },
        project: projectFromWorkspace(),
      });
    });
    this.disposables.push(sub);
  }
}

function isAntigravity(): boolean {
  // Antigravity reports a custom appName.
  return /antigravity/i.test(vscode.env.appName);
}

function isCursor(): boolean {
  return /cursor/i.test(vscode.env.appName);
}

export function activate(ctx: vscode.ExtensionContext): void {
  const bridge = new HuskBridge(ctx);
  void bridge.start();
  ctx.subscriptions.push({ dispose: () => bridge.dispose() });

  ctx.subscriptions.push(
    vscode.commands.registerCommand("husk.openStudio", () => {
      void vscode.env.openExternal(vscode.Uri.parse(huskUrl()));
    }),
  );

  ctx.subscriptions.push(
    vscode.commands.registerCommand("husk.reconnect", async () => {
      await bridge.checkConnection();
      void vscode.window.showInformationMessage("Husk: reconnect attempted.");
    }),
  );

  ctx.subscriptions.push(
    vscode.commands.registerCommand("husk.toggle", async () => {
      const cfg = vscode.workspace.getConfiguration("husk");
      const next = !(cfg.get<boolean>("captureTerminal") ?? true);
      await cfg.update(
        "captureTerminal",
        next,
        vscode.ConfigurationTarget.Global,
      );
      await bridge.checkConnection();
      void vscode.window.showInformationMessage(
        `Husk terminal capture: ${next ? "on" : "off"}.`,
      );
    }),
  );
}

export function deactivate(): void {
  // Bridge disposal handled via ctx.subscriptions.
}
