import Layout from "@theme/Layout";
import Link from "@docusaurus/Link";
import styles from "./index.module.css";

const problems = [
  {
    number: "01",
    title: "Infrastructure overhead",
    before: "Every AI agent project rebuilds prompt routing, tool dispatching, context management, and error recovery from scratch.",
    after: "Declare your agent in YAML. The framework handles routing, discovery, context, and recovery automatically.",
  },
  {
    number: "02",
    title: "Memory loss",
    before: "When the context window fills up, the agent forgets everything. Goals, progress, findings -- gone after compaction.",
    after: "Cognitive memory survives every compaction. The agent always knows its goal, its tasks, and what it found.",
  },
  {
    number: "03",
    title: "Single-threaded agents",
    before: "One agent, one context window, one task at a time. Complex work is slow and sequential.",
    after: "Spawn specialist sub-agents in parallel. Each has its own context, tools, and memory. True concurrency.",
  },
  {
    number: "04",
    title: "Tool integration pain",
    before: "MCP servers return raw, inconsistent output. Every integration requires custom parsing and error handling.",
    after: "60+ pre-configured servers. Results are normalized automatically. Smart cache, middleware, auto-reconnect.",
  },
];

const capabilities = [
  {
    category: "Agent Modules",
    items: [
      { name: "filesystem", detail: "Read, write, edit with line numbers. Surgical edits. Fast grep." },
      { name: "git", detail: "Native via pygit2. Status in 3ms. 60x faster than MCP servers." },
      { name: "web", detail: "Search (DuckDuckGo free), fetch pages, extract content." },
      { name: "database", detail: "SQLite, PostgreSQL, MySQL. Schema introspection. 29 actions." },
      { name: "shell", detail: "Commands, scripts, background tasks with output capture." },
      { name: "http", detail: "Full HTTP client. JSON API, forms, file upload, downloads." },
      { name: "notebook", detail: "Read and edit Jupyter notebooks. No kernel needed." },
      { name: "mcp", detail: "Connect 60+ external MCP servers. Plug and play." },
    ],
  },
  {
    category: "Intelligence",
    items: [
      { name: "memory", detail: "Goals, plans, tasks, sticky notes, key facts, checkpoints." },
      { name: "multi-agent", detail: "Coordinator + specialists. Parallel execution. Structured results." },
      { name: "skills", detail: "Reusable workflow commands. /commit, /review, /audit." },
      { name: "context", detail: "Auto-compaction, summary brain, memory re-injection." },
    ],
  },
  {
    category: "Infrastructure",
    items: [
      { name: "security", detail: "Risk levels, grant/deny/approve policies, approval queue." },
      { name: "middleware", detail: "App, module, MCP levels. Secret masking, content filter, RAG." },
      { name: "API", detail: "REST API, SSE streaming, multi-worker daemon, rate limiting." },
      { name: "channels", detail: "Email, Slack, Telegram, webhook, SMS. Pluggable." },
    ],
  },
];

const providers = [
  "DeepSeek", "OpenAI", "Anthropic", "Groq", "Mistral",
  "Ollama", "vLLM", "LM Studio", "Together", "OpenRouter",
];

function Hero() {
  return (
    <header className={styles.hero}>
      <div className="container">
        <div className={styles.heroInner}>
          <p className={styles.heroBadge}>Open Source AI Agent Framework</p>
          <h1 className={styles.heroTitle}>
            Stop coding agent infrastructure.
            <br />
            <span className={styles.heroHighlight}>Start building agents.</span>
          </h1>
          <p className={styles.heroSubtitle}>
            Digitorn is a declarative framework that turns a YAML file into a
            production-ready AI agent application. Tool discovery, cognitive memory,
            multi-agent orchestration, security policies -- all handled.
            You just define what your agent does.
          </p>
          <div className={styles.buttons}>
            <Link className={styles.buttonPrimary} to="/docs/app-language/getting-started">
              Get Started
            </Link>
            <Link className={styles.buttonSecondary} to="/docs">
              Read the Docs
            </Link>
          </div>
          <p className={styles.heroNote}>
            Works with any LLM. Deploy anywhere. No vendor lock-in.
          </p>
        </div>
      </div>
    </header>
  );
}

function ProblemSolution() {
  return (
    <section className={styles.problemSection}>
      <div className="container">
        <h2 className={styles.sectionTitle}>The problem we solve</h2>
        <p className={styles.sectionSubtitle}>
          Every AI agent project rebuilds the same infrastructure.
          Digitorn provides it as a declarative layer.
        </p>
        <div className={styles.problemGrid}>
          {problems.map((p, i) => (
            <div key={i} className={styles.problemCard}>
              <div className={styles.problemHeader}>
                <span className={styles.problemNumber}>{p.number}</span>
                <h3 className={styles.problemTitle}>{p.title}</h3>
              </div>
              <div className={styles.problemBody}>
                <div className={styles.problemBefore}>
                  <p>{p.before}</p>
                </div>
                <div className={styles.problemDivider} />
                <div className={styles.problemAfter}>
                  <p>{p.after}</p>
                </div>
              </div>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

function CodeDemo() {
  return (
    <section className={styles.codeSection}>
      <div className="container">
        <div className="row">
          <div className="col col--5">
            <h2 className={styles.sectionTitle}>20 lines of YAML.</h2>
            <h2 className={styles.sectionTitle2}>A complete agent.</h2>
            <p className={styles.sectionDescription}>
              This agent reads files, makes git commits, searches the web,
              tracks its tasks with a checklist, and remembers what it found
              even after the context window is compacted.
            </p>
            <ul className={styles.featureList}>
              <li>No Python code to write</li>
              <li>No prompt engineering required</li>
              <li>No infrastructure to manage</li>
              <li>Switch LLM providers in one line</li>
            </ul>
            <Link className={styles.buttonPrimary} to="/docs/app-language/getting-started">
              Build your first agent
            </Link>
          </div>
          <div className="col col--7">
            <pre className={styles.codeBlock}>
              <code>{`app:
  app_id: code-assistant
  name: "Code Assistant"

modules:
  filesystem: {}
  git: {}
  web: {}
  memory:
    config:
      working_memory: true
      todo_list: true
      checkpoint: true

agents:
  - id: assistant
    brain:
      provider: deepseek
      model: deepseek-chat
      config:
        api_key: "\u007B\u007Benv.DEEPSEEK_API_KEY\u007D\u007D"
    system_prompt: |
      You are a senior software engineer.

execution:
  mode: conversation
  greeting: "Hello! What are we building today?"`}</code>
            </pre>
          </div>
        </div>
      </div>
    </section>
  );
}

function Capabilities() {
  return (
    <section className={styles.capSection}>
      <div className="container">
        <h2 className={styles.sectionTitle}>Everything an agent needs</h2>
        <p className={styles.sectionSubtitle}>
          11 modules, 135+ actions, cognitive memory, multi-agent orchestration,
          security policies, and a production API. All opt-in.
        </p>
        <div className={styles.capGrid}>
          {capabilities.map((cat, i) => (
            <div key={i} className={styles.capCategory}>
              <h3 className={styles.capCategoryTitle}>{cat.category}</h3>
              <div className={styles.capItems}>
                {cat.items.map((item, j) => (
                  <div key={j} className={styles.capItem}>
                    <code className={styles.capName}>{item.name}</code>
                    <span className={styles.capDetail}>{item.detail}</span>
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

function Providers() {
  return (
    <section className={styles.providerSection}>
      <div className="container">
        <h2 className={styles.sectionTitle}>Any LLM. One config change.</h2>
        <p className={styles.sectionSubtitle}>
          Switch from a cloud API to a local model by changing one line.
          No code changes. No redeployment.
        </p>
        <div className={styles.providerGrid}>
          {providers.map((p, i) => (
            <div key={i} className={styles.providerBadge}>{p}</div>
          ))}
        </div>
        <p className={styles.providerNote}>
          Plus any OpenAI-compatible API. Text-based tool calling recovery
          for models with imperfect function calling support.
        </p>
      </div>
    </section>
  );
}

function CTA() {
  return (
    <section className={styles.ctaSection}>
      <div className="container">
        <h2 className={styles.ctaTitle}>Ready to build?</h2>
        <p className={styles.ctaSubtitle}>
          Install Digitorn, create a YAML file, and run your first agent in under 5 minutes.
        </p>
        <div className={styles.ctaButtons}>
          <Link className={styles.buttonPrimary} to="/docs/app-language/getting-started">
            Get Started
          </Link>
          <Link className={styles.buttonSecondary} to="/docs/modules/modules-index">
            Explore Modules
          </Link>
        </div>
      </div>
    </section>
  );
}

const docSections = [
  {
    title: "Getting Started",
    description: "Install Digitorn, create your first agent app, and run it in minutes.",
    link: "/app-language/getting-started",
  },
  {
    title: "Agents and Tools",
    description: "Configure agents, brains, tool discovery, built-in primitives, and multi-agent orchestration.",
    link: "/app-language/agents",
  },
  {
    title: "Cognitive Memory",
    description: "Goals, plans, tasks, facts, notes, and checkpoints that survive context compaction.",
    link: "/app-language/memory",
  },
  {
    title: "Security Architecture",
    description: "7 enforcement gates, approval workflows, audit log, rate limiting, and data classification.",
    link: "/app-language/security",
  },
  {
    title: "Module Reference",
    description: "All 11 modules: filesystem, git, shell, web, database, notebook, memory, MCP, and more.",
    link: "/modules/modules-index",
  },
  {
    title: "REST API",
    description: "36 endpoints, SSE streaming, session management, and deployment workflows.",
    link: "/app-language/api-integration",
  },
];

function DocLinks() {
  return (
    <section className={styles.docLinks}>
      <div className="container">
        <h2 className={styles.sectionHeading}>Explore the Documentation</h2>
        <div className={styles.docGrid}>
          {docSections.map((section, idx) => (
            <Link key={idx} to={section.link} className={styles.docCard}>
              <h3>{section.title}</h3>
              <p>{section.description}</p>
            </Link>
          ))}
        </div>
      </div>
    </section>
  );
}

export default function Home() {
  return (
    <Layout
      title="Digitorn -- Declarative AI Agent Framework"
      description="Build production-ready AI agent applications with YAML. Cognitive memory, multi-agent orchestration, 11 modules, any LLM."
    >
      <Hero />
      <main>
        <DocLinks />
        <ProblemSolution />
        <CodeDemo />
        <Capabilities />
        <Providers />
        <CTA />
      </main>
    </Layout>
  );
}
