import type { ReactNode } from "react";
import Link from "@docusaurus/Link";
import useDocusaurusContext from "@docusaurus/useDocusaurusContext";
import Layout from "@theme/Layout";
import Heading from "@theme/Heading";

import styles from "./index.module.css";

function HeroBrandMark({ size = 64 }: { size?: number }) {
  const radius = Math.round(size * 0.24);
  const inner = Math.round(size * 0.54);
  return (
    <div
      className={styles.heroBrand}
      style={{ width: size, height: size, borderRadius: radius }}
      aria-hidden="true"
    >
      <div
        className={styles.heroBrandTile}
        style={{ borderRadius: radius }}
      />
      <div
        className={styles.heroBrandGloss}
        style={{ borderRadius: radius }}
      />
      <img
        src="/img/logo.png"
        alt=""
        width={inner}
        height={inner}
        className={styles.heroBrandMark}
      />
    </div>
  );
}

function Hero() {
  const { siteConfig } = useDocusaurusContext();
  return (
    <section className={styles.hero}>
      <div className="container">
        <div className={styles.heroBrandWrap}>
          <HeroBrandMark size={72} />
        </div>
        <Heading as="h1" className={styles.heroTitle}>
          {siteConfig.title}
        </Heading>
        <p className={styles.heroSubtitle}>
          A declarative framework for building AI agent applications.
          Define what your agents do, how they think, and what tools
          they use - entirely in YAML.
        </p>
        <div className={styles.heroButtons}>
          <Link
            className={`${styles.heroCta} ${styles.heroCtaPrimary}`}
            to="/docs/language/getting-started"
          >
            Get started
            <span aria-hidden="true">→</span>
          </Link>
          <Link
            className={`${styles.heroCta} ${styles.heroCtaGhost}`}
            to="/docs/language/"
          >
            Read the language reference
          </Link>
        </div>
      </div>
    </section>
  );
}

function FeatureCard({
  title,
  description,
  href,
}: {
  title: string;
  description: string;
  href: string;
}) {
  return (
    <Link to={href} className={styles.featureCard}>
      <h3 className={styles.featureTitle}>{title}</h3>
      <p className={styles.featureDescription}>{description}</p>
    </Link>
  );
}

function Features() {
  const features = [
    {
      title: "8-block YAML",
      description:
        "One canonical schema. Every field has exactly one home. " +
        "Pydantic validates with extra: forbid, typos don't ship.",
      href: "/docs/language/grammar",
    },
    {
      title: "23 modules preinstalled",
      description:
        "Filesystem, shell, web, database, memory, RAG, MCP, " +
        "channels, widgets, workspace. Drop them under tools.modules.",
      href: "/docs/reference/modules/",
    },
    {
      title: "Multi-provider brains",
      description:
        "OpenAI, Anthropic, DeepSeek, Groq, Ollama, vLLM, LM Studio, " +
        "and 10+ more. Native or text-based tool calling, auto-detected.",
      href: "/docs/language/agents",
    },
    {
      title: "Security in three layers",
      description:
        "Capabilities (grant / deny / approve), behavior engine " +
        "(14 built-in rules), OS-level sandbox (Landlock / seccomp).",
      href: "/docs/language/security",
    },
    {
      title: "Multi-agent built-in",
      description:
        "Coordinator + specialists, parallel sub-agent orchestration, " +
        "isolated contexts, eight Agent() modes via one tool.",
      href: "/docs/language/multi-agent",
    },
    {
      title: "Background, channels, flows",
      description:
        "Cron, webhooks, file-watch, email, Slack, Telegram, RSS. " +
        "11 channel adapters, declarative orchestration graphs.",
      href: "/docs/language/channels",
    },
  ];
  return (
    <section className={styles.features}>
      <div className="container">
        <div className={styles.featureGrid}>
          {features.map((f) => (
            <FeatureCard key={f.title} {...f} />
          ))}
        </div>
      </div>
    </section>
  );
}

function QuickExample() {
  return (
    <section className={styles.example}>
      <div className="container">
        <div className={styles.exampleGrid}>
          <div>
            <Heading as="h2" className={styles.exampleTitle}>
              An app in twenty lines
            </Heading>
            <p className={styles.exampleLead}>
              Drop this YAML next to a daemon and chat with it. The
              same schema scales to multi-agent coordinators with
              channels, sub-agent fan-out, hooks, and credentials.
            </p>
            <div className={styles.exampleBullets}>
              <div>
                <span className={styles.bulletDot} />
                <span>
                  <strong>Declare, don't code.</strong> Modules, tools,
                  policies, hooks all in YAML.
                </span>
              </div>
              <div>
                <span className={styles.bulletDot} />
                <span>
                  <strong>One canonical grammar.</strong> Eight blocks,
                  formal spec, frozen v1.
                </span>
              </div>
              <div>
                <span className={styles.bulletDot} />
                <span>
                  <strong>Live event stream.</strong> Socket.IO with
                  per-turn snapshots and replay.
                </span>
              </div>
            </div>
            <div className={styles.exampleActions}>
              <Link
                to="/docs/language/getting-started"
                className={styles.exampleLink}
              >
                Walk through the tutorial →
              </Link>
            </div>
          </div>
          <div className={styles.exampleCode}>
            <div className={styles.exampleCodeHeader}>
              <span className={styles.exampleCodeDot} />
              <span className={styles.exampleCodeDot} />
              <span className={styles.exampleCodeDot} />
              <span className={styles.exampleCodeFile}>hello.yaml</span>
            </div>
            <pre className={styles.exampleCodeBody}>
              <code>{`app:
  app_id: hello
  name: "Hello"

agents:
  - id: assistant
    role: assistant
    brain:
      provider: ollama
      model: qwen25-7b-gpu:latest
      backend: openai_compat
      config:
        base_url: http://localhost:11434/v1
        api_key: ollama
    system_prompt: |
      You are a helpful assistant.
      Reply concisely.

tools:
  modules:
    memory: {}
  capabilities:
    default_policy: auto`}</code>
            </pre>
          </div>
        </div>
      </div>
    </section>
  );
}

function Stability() {
  return (
    <section className={styles.stability}>
      <div className="container">
        <div className={styles.stabilityCard}>
          <div className={styles.stabilityCol}>
            <Heading as="h3" className={styles.stabilityTitle}>
              Documentation as a contract
            </Heading>
            <p className={styles.stabilityBody}>
              Claims in this documentation are cross-checked against
              the source code, and YAML examples are deployed against a
              live daemon before they ship. If you spot a divergence
              between what's written here and the running system, the
              doc is the bug; open an issue.
            </p>
          </div>
          <div className={styles.stabilityCol}>
            <Heading as="h3" className={styles.stabilityTitle}>
              v1 stability
            </Heading>
            <p className={styles.stabilityBody}>
              The 8-block YAML is frozen. Required fields are not
              added, existing field types are not narrowed, and
              default values stay the same across minor and patch
              releases. New optional fields land additively.
            </p>
            <Link to="/docs/versioning" className={styles.exampleLink}>
              Read the stability guarantees →
            </Link>
          </div>
        </div>
      </div>
    </section>
  );
}

export default function Home(): ReactNode {
  const { siteConfig } = useDocusaurusContext();
  return (
    <Layout
      title="Home"
      description={
        siteConfig.tagline ||
        "Digitorn - declarative AI agent framework. Build apps in YAML."
      }
    >
      <main>
        <Hero />
        <Features />
        <QuickExample />
        <Stability />
      </main>
    </Layout>
  );
}
