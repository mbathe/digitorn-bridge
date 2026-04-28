---
version: 1
description: How to add a live preview to an app
---

## Preview skill

### When to add a preview

Add a preview when the app generates visual output:
- **Code generator** (Lovable-style) → render_mode: react
- **Slide maker** → render_mode: slides
- **Document builder** → render_mode: html / markdown / latex
- **Workflow canvas** → render_mode: builder
- **Dashboard** → render_mode: auto

Do NOT add preview for: chatbots, Q&A, data analysis (text-only output).

### YAML blocks needed (3 blocks)

**1. modules.workspace** - agent's file API:
```yaml
modules:
  workspace:
    config:
      render_mode: react
      entry_file: src/App.tsx
      sync_to_disk: true
      lint: true
      instructions: |
        You generate React + Tailwind code.
        Write to src/App.tsx for the main component.
  preview: {}
```

**2. workspace** - top-level block for the Flutter client:
```yaml
workspace:
  render_mode: react
  entry_file: src/App.tsx
  title: "My App"
```

**3. preview** - static mode (no dev server):
```yaml
preview:
  enabled: false
```

**4. capabilities** - grant workspace actions:
```yaml
capabilities:
  grant:
    - module: workspace
      actions: [write, read, edit, glob, grep, delete]
```

### render_mode values

| Mode | Client renders |
|------|---------------|
| react | React components in WebView |
| builder | n8n-style flow canvas |
| html | Raw HTML in iframe |
| markdown | Markdown native rendering |
| slides | Slide deck (each .md = one slide) |
| code | Syntax highlighting only |
| latex | LaTeX → PDF rendering |
| auto | Detect from first file |

### Building the preview client

The preview is a React app that uses @digitorn/preview-sdk.

**Step 1 - Write the source files:**
```
workspace.write("preview/package.json", <package_json>)
workspace.write("preview/tsconfig.json", <tsconfig>)
workspace.write("preview/vite.config.ts", <vite_config>)
workspace.write("preview/index.html", <index_html>)
workspace.write("preview/src/main.tsx", <main_tsx>)
workspace.write("preview/src/App.tsx", <app_tsx>)
```

**package.json template:**
```json
{
  "name": "preview",
  "type": "module",
  "scripts": { "dev": "vite", "build": "tsc -b && vite build" },
  "dependencies": {
    "@digitorn/preview-sdk": "file:../../../../digitorn-preview-sdk",
    "react": "^18.3.1",
    "react-dom": "^18.3.1"
  },
  "devDependencies": {
    "@types/react": "^18.3.12",
    "@types/react-dom": "^18.3.1",
    "@vitejs/plugin-react": "^4.3.3",
    "typescript": "^5.6.3",
    "vite": "^5.4.10"
  }
}
```

**main.tsx template:**
```tsx
import React from "react";
import ReactDOM from "react-dom/client";
import { DigiPreview } from "@digitorn/preview-sdk";
import App from "./App";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <DigiPreview><App /></DigiPreview>
);
```

**App.tsx template (developer writes this):**
```tsx
import { useFiles, useConnection, useAgentStatus } from "@digitorn/preview-sdk";

export default function App() {
  const files = useFiles();
  const connected = useConnection();
  const status = useAgentStatus();
  
  return (
    <div>
      <p>{connected ? "live" : "reconnecting"} · {status}</p>
      {Array.from(files.entries()).map(([path, file]) => (
        <div key={path}>
          <h3>{path} ({file.language})</h3>
          <pre>{file.content}</pre>
        </div>
      ))}
    </div>
  );
}
```

**Step 2 - Install and build:**
```
shell.bash(command="cd preview && npm install")
shell.bash(command="cd preview && npm run build")
```

**Step 3 - Verify:**
```
workspace.glob("preview/dist/**/*")
```

### SDK hooks available

| Hook | Returns | Usage |
|------|---------|-------|
| useFiles() | Map<path, WorkspaceFile> | All files |
| useFile(path) | string | One file content |
| useFileJson(path) | T | Parsed JSON file |
| useFileStats() | FileStats | Global change stats |
| useConnection() | boolean | Connected? |
| useAgentStatus() | AgentStatus | idle/thinking/working |
| useNodes() | PreviewNode[] | Canvas nodes |
| useEdges() | PreviewEdge[] | Canvas edges |

### Workspace isolation

When sync_to_disk is true, files are isolated per session:
1. YAML sync_path → fixed path
2. User-selected workspace folder → user's project
3. Auto: `~/.digitorn/workspaces/{app_id}/{session_id}/`

Multiple users never overwrite each other.
