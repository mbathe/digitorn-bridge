import type {ReactNode} from 'react';
import clsx from 'clsx';
import Heading from '@theme/Heading';
import styles from './styles.module.css';

type FeatureItem = {
  title: string;
  icon: string;
  description: ReactNode;
};

const FeatureList: FeatureItem[] = [
  {
    title: 'Declarative YAML Apps',
    icon: '\u{1F4DD}',
    description: (
      <>
        Define complete AI applications in YAML. Agents, tools, memory, flows,
        triggers, and security — no code required. Run with a single command.
      </>
    ),
  },
  {
    title: '18+ Modules, 280+ Actions',
    icon: '\u{1F9E9}',
    description: (
      <>
        Filesystem, browser, database, Excel, Word, PowerPoint, GUI automation,
        vision, IoT, and more. Each module is isolated, versioned, and secure.
      </>
    ),
  },
  {
    title: 'Two Execution Modes',
    icon: '\u{26A1}',
    description: (
      <>
        <strong>Agentique Mode</strong>: the LLM decides autonomously.{' '}
        <strong>Compiler Mode</strong> (IML Protocol): you define the exact plan.
        Same modules, same security, different control.
      </>
    ),
  },
  {
    title: 'Multi-Agent Orchestration',
    icon: '\u{1F916}',
    description: (
      <>
        Run multiple agents with delegation, async spawning, and message passing.
        18 flow step types including parallel, branch, loop, map/reduce, and race.
      </>
    ),
  },
  {
    title: 'Defense-in-Depth Security',
    icon: '\u{1F6E1}\u{FE0F}',
    description: (
      <>
        15-step security pipeline with 4 profiles, 32 permissions, intent
        verification, sandboxing, approval flows, and full audit trail.
      </>
    ),
  },
  {
    title: '5-Level Memory System',
    icon: '\u{1F9E0}',
    description: (
      <>
        Working, conversation, episodic, project, and procedural memory.
        Automatic context management with token budgets, compression, and
        on-demand retrieval.
      </>
    ),
  },
];

function Feature({title, icon, description}: FeatureItem) {
  return (
    <div className={clsx('col col--4')}>
      <div className="text--center padding-horiz--md">
        <div className={styles.featureIcon}>{icon}</div>
        <Heading as="h3">{title}</Heading>
        <p>{description}</p>
      </div>
    </div>
  );
}

export default function HomepageFeatures(): ReactNode {
  return (
    <section className={styles.features}>
      <div className="container">
        <div className="row">
          {FeatureList.map((props, idx) => (
            <Feature key={idx} {...props} />
          ))}
        </div>
      </div>
    </section>
  );
}
