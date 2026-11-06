export interface StudyConfig {
  version: 1;
  id: string;
  goal: string;
  constraints: string[];
  model: { provider: string; id: string; thinking: "off" | "medium" };
  authorization: {
    id: string;
    expiresAt: number;
    slots: string[];
    jobSeconds: number;
  };
  maxRounds: number;
  minimumRounds?: number;
  execution?: { id: string; version: string; identity: string };
  source: { root: string; files: string[] };
  inputs: {
    root: string;
    version: string;
    feedback: "development" | "final-only";
  };
  evaluation: {
    id: string;
    version: string;
    description?: string;
    // The built-in CPU path uses these commands; an injected backend owns execution.
    predictCommand: string[];
    command: string[];
    feedback: "development" | "final-only";
  };
  // Runtime paths are resolved at initialization and never relocated on recovery.
  python: string;
  tasks: ResearchTask[];
}

export interface ResearchTask {
  id: string;
  source: "human" | "autonomous";
  question: string;
  constraints: string[];
  basedOn: string[];
}

export interface Plan {
  question: string;
  change: string;
  rationale: string;
  basedOn: string[];
}

export interface Receipt {
  id: string;
  authorization: string;
  slot: string;
  candidate: string;
  inputVersion: string;
  evaluation: string;
  evaluationVersion: string;
  feedback: "development";
  jobId: string;
  backend?: {
    id: string;
    version: string;
    identity: string;
    operationId: string;
  };
  executionComplete: boolean;
  valid: boolean;
  metrics: Record<string, number>;
  observations: string[];
  rawResult: string;
  candidatePath: string;
}

export interface ExperimentBackend {
  execution: { id: string; version: string; identity: string };
  run(
    root: string,
    config: StudyConfig,
    workspace: string,
    slot: string,
  ): Promise<Receipt>;
  readReceipts(root: string, config: StudyConfig): Receipt[];
}

export interface Review {
  interpretation: string;
  limitations: string[];
  knowledgeChanged: boolean;
  goal: "achieved" | "not-achieved" | "unknown";
  continue: boolean;
  nextQuestion: string;
}

export interface ResearchRecord {
  id: string;
  task: ResearchTask;
  plan: Plan;
  execute: string;
  receipts: Receipt[];
  review: Review;
  conversations: { plan: number; execute: number; review: number };
  createdAt: string;
}
