export interface User {
  sub: string;
  username: string;
  email?: string | null;
  name?: string | null;
  roles: string[];
  display_roles: string[];
  can_approve: boolean;
  can_initiate: boolean;
  can_execute: boolean;
  org?: string | null;
}

export type ThemeMode = "dark" | "light" | "system";
export type ResolvedTheme = "dark" | "light";

export type NavKey =
  | "workspace"
  | "projects"
  | "infrastructure"
  | "incidents"
  | "knowledge"
  | "analytics"
  | "admin"
  | "settings";

export type ArtifactTab =
  | "timeline"
  | "reasoning"
  | "terraform"
  | "logs"
  | "metrics"
  | "traces"
  | "references"
  | "approvals";

export type MenuKey =
  | "org"
  | "cloud"
  | "env"
  | "region"
  | "model"
  | "role"
  | "notif"
  | "profile"
  | "theme"
  | null;

export type ApprovalState = "pending" | "approved" | "rejected";

export interface ChatStep {
  label: string;
}

export interface Reference {
  title: string;
  source?: string | null;
  kind?: string | null;
  url?: string | null;
  relevance?: number | null;
}

export interface Analysis {
  summary?: string;
  cards?: { title: string; conf?: string; body: string }[];
}

export interface ParamRequestItem {
  name: string;
  label: string;
  kind?: string;
  choices?: string[] | null;
  help?: string;
}
export interface ParamRequest {
  template: string;
  items: ParamRequestItem[];
  collected?: Record<string, unknown>;
}

export interface ChatMessage {
  id: string;
  isUser?: boolean;
  isAI?: boolean;
  text: string;
  streaming?: boolean;
  showTimeline?: boolean;
  stepIdx?: number;
  steps?: ChatStep[];
  // live backend-bound fields
  runId?: string;
  messageId?: string;
  analysis?: Analysis;
  references?: Reference[];
  confidentiality?: { level: string; score: number };
  intent?: string;
  workflow?: string;
  paramRequest?: ParamRequest | null;
  interrupt?: Record<string, unknown> | null;
  consoleLines?: { stream: string; line: string }[];
  error?: string | null;
  done?: boolean;
  // Names of sensitive Terraform outputs revealable ONCE via POST /runs/{id}/credentials (N-02).
  sensitiveOutputs?: string[];
  tab?: "conversation" | "analysis";
}

export interface SelectOption {
  label: string;
  sub?: string;
  dot?: string;
}

export interface SessionMeta {
  id: string;
  title: string;
  status: string;
  created_at: string;
}

export interface Overview {
  org: { name: string; plan?: string; member_count?: number };
  projects: number;
  incidents: number;
}
