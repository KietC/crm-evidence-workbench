export interface AdapterTab {
  id: string;
  labels: string[];
}

export interface EndpointContract {
  id: string;
  method: string;
  path: string;
  min: number;
}

export interface OkkiAdapter {
  schema: number;
  adapter_id: string;
  updated_at: string;
  origin: string;
  customer_path: string;
  customer_list_url: string;
  customer_queue: {
    stage_title: string;
    list_endpoint: string;
    page_size: number;
    safety_page_cap: number;
  };
  page_asset_hosts: string[];
  external_ai_blocked_hosts: string[];
  root_tabs: AdapterTab[];
  dynamic: {
    history_labels: string[];
    filter_labels: string[][];
    mail_labels: string[];
    next_labels: string[];
    trail_endpoint: string;
    mail_info_endpoint: string;
    mail_track_endpoint: string;
    mail_module_values: string[];
    visible_page_cap: number;
    safety_page_cap: number;
  };
  documents: {
    list_endpoint: string;
    folder_endpoint: string;
    next_labels: string[];
    visible_page_cap: number;
    safety_page_cap: number;
  };
  expected_endpoint_contracts: EndpointContract[];
  known_count_paths: Record<string, string[]>;
}

export type JobPhase =
  | "idle"
  | "preflight"
  | "root"
  | "tabs"
  | "mail"
  | "documents"
  | "reconcile"
  | "complete"
  | "incomplete"
  | "failed"
  | "cancelled";

export interface JobStatus {
  id: string | null;
  phase: JobPhase;
  running: boolean;
  progress: number;
  headline: string;
  detail: string;
  companyId: string | null;
  caseRoot: string | null;
  sessionRoot: string | null;
  startedAt: string | null;
  finishedAt: string | null;
  errors: string[];
  warnings: string[];
  metrics: Record<string, number | string | boolean | null>;
}

export interface StoredResponse {
  sequence: number;
  method: string;
  url: string;
  path: string;
  status: number;
  resourceType: string;
  mimeType: string;
  bodyRelativePath: string | null;
  bodySha256: string | null;
  bodyBytes: number | null;
  requestBodySha256?: string | null | undefined;
  json: unknown | null;
  error: string | null;
  responseHeaders?: Record<string, string> | undefined;
  statusText?: string | undefined;
  serverAddress?: { ipAddress: string; port: number } | null | undefined;
  securityDetails?: Record<string, unknown> | null | undefined;
  timing?: Record<string, number> | undefined;
  fromServiceWorker?: boolean | undefined;
}

export interface ReconciliationCheck {
  id: string;
  title: string;
  status: "pass" | "fail" | "warning" | "not_observed";
  expected: unknown;
  actual: unknown;
  evidence: string[];
  detail: string;
}
