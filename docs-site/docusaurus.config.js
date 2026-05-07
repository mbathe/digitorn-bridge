// @ts-check
const { themes: prismThemes } = require("prism-react-renderer");

/** @type {import('@docusaurus/types').Config} */
const config = {
  title: "Digitorn",
  tagline: "Declarative AI agent framework",
  favicon: "img/favicon.png",

  url: "https://docs.digitorn.dev",
  baseUrl: "/",

  organizationName: "digitorn",
  projectName: "digitorn-bridge",

  onBrokenLinks: "warn",
  onBrokenAnchors: "warn",

  i18n: {
    defaultLocale: "en",
    locales: ["en"],
  },

  markdown: {
    mermaid: true,
    hooks: {
      onBrokenMarkdownLinks: "warn",
    },
  },

  themes: ["@docusaurus/theme-mermaid"],

  presets: [
    [
      "classic",
      /** @type {import('@docusaurus/preset-classic').Options} */
      ({
        docs: {
          path: "docs",
          routeBasePath: "/docs",
          sidebarPath: "./sidebars.js",
          editUrl:
            "https://github.com/digitorn/digitorn-bridge/tree/main/docs-site/",
          exclude: ["_internal/**", "archive/**", "**/_drafts/**"],
        },
        blog: false,
        theme: {
          customCss: "./src/css/custom.css",
        },
      }),
    ],
  ],

  themeConfig:
    /** @type {import('@docusaurus/preset-classic').ThemeConfig} */
    ({
      image: "img/digitorn-social.png",

      metadata: [
        {
          name: "description",
          content:
            "Digitorn - declarative AI agent framework. Build apps in YAML.",
        },
        { name: "theme-color", content: "#A78BFA" },
      ],

      navbar: {
        title: "Digitorn",
        logo: {
          alt: "Digitorn",
          src: "img/logo.png",
          width: 26,
          height: 26,
          className: "brand-logo",
        },
        hideOnScroll: false,
        items: [
          {
            type: "docSidebar",
            sidebarId: "guideSidebar",
            position: "left",
            label: "Language",
          },
          {
            type: "docSidebar",
            sidebarId: "modulesSidebar",
            position: "left",
            label: "Reference",
          },
          {
            to: "/docs/tutorial/",
            position: "left",
            label: "Tutorial",
          },
          {
            to: "/docs/concepts/",
            position: "left",
            label: "Concepts",
          },
          {
            href: "https://github.com/digitorn/digitorn-bridge",
            position: "right",
            className: "header-github-link",
            "aria-label": "GitHub repository",
          },
        ],
      },

      footer: {
        style: "light",
        logo: {
          alt: "Digitorn",
          src: "img/logo.png",
          width: 30,
          height: 30,
          className: "brand-logo",
        },
        links: [
          {
            title: "Documentation",
            items: [
              { label: "Getting started", to: "/docs/language/getting-started" },
              { label: "Language reference", to: "/docs/language/" },
              { label: "Module reference", to: "/docs/reference/modules/" },
              { label: "Concepts", to: "/docs/concepts/" },
            ],
          },
          {
            title: "Surfaces",
            items: [
              { label: "CLI", to: "/docs/reference/cli/" },
              { label: "Socket.IO", to: "/docs/reference/api/socketio" },
              { label: "Client SDKs", to: "/docs/reference/client-sdks/" },
              { label: "Deployment", to: "/docs/deployment/" },
            ],
          },
          {
            title: "Project",
            items: [
              { label: "Versioning", to: "/docs/versioning" },
              { label: "GitHub", href: "https://github.com/digitorn/digitorn-bridge" },
              { label: "Issues", href: "https://github.com/digitorn/digitorn-bridge/issues" },
            ],
          },
        ],
        copyright: `© ${new Date().getFullYear()} Digitorn`,
      },

      prism: {
        theme: prismThemes.oneLight,
        darkTheme: prismThemes.oneDark,
        additionalLanguages: [
          "bash",
          "yaml",
          "toml",
          "json",
          "diff",
          "ini",
          "docker",
          "python",
          "tsx",
        ],
      },

      mermaid: {
        // Force the dark palette in both color modes so diagrams stay
        // legible whether the docs page is light or dark - they are
        // self-contained dark cards either way.
        theme: { light: "dark", dark: "dark" },
        options: {
          fontFamily:
            "Inter, -apple-system, BlinkMacSystemFont, sans-serif",
          themeVariables: {
            // Brand palette
            primaryColor: "#1A1A1A",
            primaryTextColor: "#E6E6E6",
            primaryBorderColor: "#A78BFA",
            secondaryColor: "#141414",
            secondaryTextColor: "#E6E6E6",
            secondaryBorderColor: "#3B82F6",
            tertiaryColor: "#0D0D0D",
            tertiaryTextColor: "#E6E6E6",
            tertiaryBorderColor: "#3B82F6",
            // Edges and labels
            lineColor: "#7C7C8A",
            textColor: "#E6E6E6",
            edgeLabelBackground: "#0D0D0D",
            // Default node colors (used when no classDef is applied)
            mainBkg: "#1A1A1A",
            secondBkg: "#141414",
            nodeBorder: "#A78BFA",
            nodeTextColor: "#E6E6E6",
            // Subgraph / cluster colors
            clusterBkg: "rgba(167, 139, 250, 0.05)",
            clusterBorder: "#3B82F6",
            titleColor: "#E6E6E6",
            // Sequence diagram specifics
            actorBkg: "#1A1A1A",
            actorBorder: "#A78BFA",
            actorTextColor: "#E6E6E6",
            actorLineColor: "#7C7C8A",
            signalColor: "#E6E6E6",
            signalTextColor: "#E6E6E6",
            labelBoxBkgColor: "#1A1A1A",
            labelBoxBorderColor: "#A78BFA",
            labelTextColor: "#E6E6E6",
            loopTextColor: "#E6E6E6",
            noteBkgColor: "#0D0D0D",
            noteBorderColor: "#3B82F6",
            noteTextColor: "#E6E6E6",
            activationBkgColor: "#1A1A1A",
            activationBorderColor: "#A78BFA",
            sequenceNumberColor: "#0D0D0D",
            // Page background stays transparent so the card sits on
            // the docs surface without a hard rectangle.
            background: "transparent",
          },
        },
      },

      colorMode: {
        defaultMode: "dark",
        disableSwitch: false,
        respectPrefersColorScheme: true,
      },

      tableOfContents: {
        minHeadingLevel: 2,
        maxHeadingLevel: 4,
      },

      docs: {
        sidebar: {
          hideable: true,
          autoCollapseCategories: false,
        },
      },
    }),
};

module.exports = config;
