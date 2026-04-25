import type {ReactNode} from 'react';
import clsx from 'clsx';
import Link from '@docusaurus/Link';
import useDocusaurusContext from '@docusaurus/useDocusaurusContext';
import Layout from '@theme/Layout';
import HomepageFeatures from '@site/src/components/HomepageFeatures';
import Heading from '@theme/Heading';

import styles from './index.module.css';

function HomepageHeader() {
  const {siteConfig} = useDocusaurusContext();
  return (
    <header className={clsx('hero', styles.heroBanner)}>
      <div className="container">
        <Heading as="h1" className={styles.heroTitle}>
          {siteConfig.title}
        </Heading>
        <p className={styles.heroSubtitle}>{siteConfig.tagline}</p>
        <p className={styles.heroDescription}>
          Define agents, tools, memory, flows, triggers, and security —
          all in declarative YAML. 18+ modules, 280+ actions, zero boilerplate.
        </p>
        <div className={styles.buttons}>
          <Link
            className="button button--primary button--lg"
            to="/docs/app-language/getting-started">
            Get Started
          </Link>
          <Link
            className="button button--outline button--lg"
            to="/docs/overview/architecture">
            Learn the Architecture
          </Link>
        </div>
      </div>
    </header>
  );
}

function QuickExample() {
  return (
    <section className={styles.quickExample}>
      <div className="container">
        <div className="row">
          <div className="col col--6">
            <Heading as="h2">Build AI apps in YAML</Heading>
            <p>
              The App Language lets you define complete AI applications declaratively.
              Configure agents, wire tools from 18+ modules, set up multi-level memory,
              and orchestrate complex flows — without writing a single line of code.
            </p>
            <ul>
              <li>Single or multi-agent orchestration</li>
              <li>18 flow step types (parallel, branch, loop, map/reduce...)</li>
              <li>5 memory levels (working, conversation, episodic, project, procedural)</li>
              <li>Built-in security with 4 profiles and 32 permissions</li>
            </ul>
          </div>
          <div className="col col--6">
            <div className={styles.codeBlock}>
              <pre>
                <code>{`app:
  name: my-assistant
  version: "1.0"

agent:
  brain:
    provider: anthropic
    model: claude-sonnet-4-20250514
  system_prompt: |
    You are a helpful coding assistant.
  tools:
    - module: filesystem
      action: read_file
    - module: os_exec
      action: run_command

triggers:
  - type: cli
    mode: conversation

security:
  profile: power_user`}</code>
              </pre>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}

export default function Home(): ReactNode {
  const {siteConfig} = useDocusaurusContext();
  return (
    <Layout
      title="Home"
      description="LLMOS Bridge — Bridge Large Language Models to the Operating System. Declarative YAML-based AI application framework.">
      <HomepageHeader />
      <main>
        <HomepageFeatures />
        <QuickExample />
      </main>
    </Layout>
  );
}
