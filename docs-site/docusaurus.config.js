// @ts-check
const { themes: prismThemes } = require("prism-react-renderer");

/** @type {import('@docusaurus/types').Config} */
const config = {
  title: "Digitorn",
  tagline: "Declarative AI Agent Framework",
  favicon: "img/favicon.ico",

  url: "https://docs.digitorn.dev",
  baseUrl: "/",

  organizationName: "digitorn",
  projectName: "digitorn-bridge",

  onBrokenLinks: "warn",
  onBrokenMarkdownLinks: "warn",

  i18n: {
    defaultLocale: "en",
    locales: ["en"],
  },

  markdown: {
    mermaid: true,
  },

  themes: ["@docusaurus/theme-mermaid"],

  presets: [
    [
      "classic",
      /** @type {import('@docusaurus/preset-classic').Options} */
      ({
        docs: {
          path: "../docs",
          routeBasePath: "/docs",
          sidebarPath: "./sidebars.js",
          editUrl: "https://github.com/digitorn/digitorn-bridge/tree/main/docs-site/",
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

      navbar: {
        title: "Digitorn",
        logo: {
          alt: "Digitorn",
          src: "img/logo.png",
          width: 32,
          height: 32,
        },
        items: [
          {
            type: "docSidebar",
            sidebarId: "guideSidebar",
            position: "left",
            label: "Guides",
          },
          {
            type: "docSidebar",
            sidebarId: "modulesSidebar",
            position: "left",
            label: "Modules",
          },
          {
            href: "https://github.com/digitorn/digitorn-bridge",
            label: "GitHub",
            position: "right",
          },
        ],
      },

      footer: {
        style: "dark",
        links: [
          {
            title: "Documentation",
            items: [
              { label: "Getting Started", to: "/docs/app-language/getting-started" },
              { label: "Module Reference", to: "/docs/modules/modules-index" },
              { label: "API Reference", to: "/docs/app-language/api-integration" },
            ],
          },
          {
            title: "Community",
            items: [
              { label: "GitHub", href: "https://github.com/digitorn/digitorn-bridge" },
              { label: "Issues", href: "https://github.com/digitorn/digitorn-bridge/issues" },
            ],
          },
        ],
        copyright: `Copyright ${new Date().getFullYear()} Digitorn.`,
      },

      prism: {
        theme: prismThemes.github,
        darkTheme: prismThemes.dracula,
        additionalLanguages: ["bash", "yaml", "toml", "json"],
      },

      mermaid: {
        theme: {
          light: "neutral",
          dark: "dark",
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
    }),
};

module.exports = config;
