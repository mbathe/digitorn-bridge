/** @type {import('@docusaurus/plugin-content-docs').SidebarsConfig} */
const sidebars = {
  guideSidebar: [
    {
      type: "doc",
      id: "index",
      label: "Overview",
    },
    {
      type: "category",
      label: "Getting Started",
      collapsed: false,
      items: [
        "app-language/getting-started",
        "app-language/app-config",
        "app-language/expressions",
      ],
    },
    {
      type: "category",
      label: "Agents",
      items: [
        "app-language/agents",
        "app-language/tools",
        "app-language/04b-builtin-tools",
        "app-language/04c-primitives",
        "app-language/multi-agent",
      ],
    },
    {
      type: "category",
      label: "Agent Intelligence",
      items: [
        "app-language/memory",
        "app-language/context-management",
        "app-language/skills",
      ],
    },
    {
      type: "category",
      label: "Integrations",
      items: [
        "app-language/04d-mcp",
        "app-language/channels",
        "app-language/middleware",
        "app-language/composition",
      ],
    },
    {
      type: "category",
      label: "Modules",
      items: [
        "app-language/git",
        "app-language/web",
        "app-language/notebook",
      ],
    },
    {
      type: "category",
      label: "Security",
      items: [
        "app-language/security",
        "app-language/auth",
      ],
    },
    {
      type: "category",
      label: "Deployment",
      items: [
        "app-language/api-integration",
      ],
    },
    {
      type: "category",
      label: "Examples",
      items: [
        "app-language/examples",
      ],
    },
    {
      type: "category",
      label: "Planned Features",
      collapsed: true,
      items: [
        "app-language/flows",
        "app-language/macros",
        "app-language/triggers",
        "app-language/app-as-mcp-server",
      ],
    },
  ],

  modulesSidebar: [
    {
      type: "doc",
      id: "modules/modules-index",
      label: "Module System",
    },
    {
      type: "category",
      label: "Core Modules",
      collapsed: false,
      items: [
        "modules/reference/filesystem",
        "modules/reference/database",
        "modules/reference/git",
        "modules/reference/shell",
        "modules/reference/http",
        "modules/reference/web",
        "modules/reference/notebook",
      ],
    },
    {
      type: "category",
      label: "Agent Intelligence",
      items: [
        "modules/reference/memory",
        "modules/reference/agent_spawn",
      ],
    },
    {
      type: "category",
      label: "Integration",
      items: [
        "modules/reference/mcp",
        "modules/reference/hello",
      ],
    },
    {
      type: "category",
      label: "System (Internal)",
      collapsed: true,
      items: [
        "modules/reference/context_builder",
        "modules/reference/llm_provider",
        "modules/reference/index-module",
      ],
    },
  ],
};

module.exports = sidebars;
