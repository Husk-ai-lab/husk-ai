// `husk-cursor-hook install [path]` — writes .cursor/hooks.json into the project.

import { existsSync, mkdirSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";

// Inlined template (single source of truth, also written to src/templates/hooks.json for editors).
// Observability-only: we register Cursor's fire-and-forget events so Husk can
// render file edits and stop signals on the timeline. No blocking hooks.
const TEMPLATE = `{
  "version": 1,
  "hooks": {
    "afterFileEdit": [
      { "command": "husk-cursor-hook hook --event=afterFileEdit", "timeout": 5 }
    ],
    "stop": [
      { "command": "husk-cursor-hook hook --event=stop", "timeout": 5 }
    ]
  }
}
`;

export async function install(targetDir?: string): Promise<void> {
  const root = resolve(targetDir || process.cwd());
  const cursorDir = join(root, ".cursor");
  const out = join(cursorDir, "hooks.json");

  mkdirSync(cursorDir, { recursive: true });

  if (existsSync(out)) {
    process.stderr.write(
      `husk-cursor-hook: ${out} already exists — refusing to overwrite. Remove it or edit by hand.\n`,
    );
    process.exit(1);
  }

  writeFileSync(out, TEMPLATE, "utf8");
  process.stdout.write(
    `Wrote ${out}\n\nNext:\n  1. Start Husk (in another terminal):  uv run husk-studio start\n  2. Open the Studio:  http://localhost:7654\n  3. Verify the bridge:  husk-cursor-hook ping\n  4. Open this project in Cursor — file edits and stop signals will stream into the Studio timeline.\n`,
  );
}
