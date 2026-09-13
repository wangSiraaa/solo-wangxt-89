export interface Issue {
  issue_key: string;
  kind: string;
  title: string;
  competitor_id: number | null;
  read_id: number | null;
  detail: any;
  options: string[];
  resolution: {
    decision: string;
    decision_label: string;
    reason: string;
    decided_by: string;
    decided_at: string;
    payload: any;
  } | null;
}

export interface Segment {
  node_code: string;
  node_name: string;
  kind: string;
  time: string;
  read_id: number;
  chip: string;
  gap_s: number;
  feasible: boolean;
  note: string | null;
}

export interface Lap {
  lap_no: number;
  start_time: string | null;
  start_basis: string;
  finish_time: string;
  net_s: number;
  gun_elapsed_s: number;
  segments: Segment[];
  missing_nodes: string[];
  issue_keys: string[];
  status: 'confirmed' | 'in_review' | 'void' | 'in_progress';
}

export interface TimelineEvent {
  read_id: number;
  chip: string;
  node_code: string;
  time: string;
  device_id: string;
  raw_seq: number;
  treatment:
    | 'accepted'
    | 'held_review'
    | 'rejected'
    | 'duplicate_dropped'
    | 'unknown_node_dropped';
  issue_key: string | null;
}

export interface ResultRow {
  competitor_id: number;
  bib: string;
  name: string;
  category_id: number;
  category_name: string;
  mixed: boolean;
  class_label: string | null;
  rank_by: string;
  laps_required: number;
  gun_time: string;
  net_start_time: string;
  net_start_basis: string;
  laps: Lap[];
  partial_lap: Lap | null;
  chip_switches: { at: string; from_chip: string; to_chip: string }[];
  confirmed_laps: number;
  extra_laps: number;
  final_finish_time: string | null;
  total_net_s: number | null;
  total_gun_s: number | null;
  open_issue_keys: string[];
  status:
    | 'FINISHER'
    | 'FINISHER_PENDING_REVIEW'
    | 'IN_REVIEW'
    | 'DNF'
    | 'DQ';
  timeline: TimelineEvent[];
}

export interface LeaderboardEntry {
  competitor_id: number;
  bib: string;
  name: string;
  status: string;
  class_label: string | null;
  laps: number;
  total_net_s: number | null;
  total_gun_s: number | null;
  rank: number | null;
  class_rank: number | null;
}

export interface Replay {
  algo_version: string;
  results: ResultRow[];
  issues: Issue[];
  leaderboard: { category_id: number; entries: LeaderboardEntry[] }[];
}

export interface ReplayEnvelope {
  input_hash: string;
  output_hash: string;
  output: Replay;
}

export interface EventSummary {
  id: number;
  name: string;
  created_at: string | null;
}

export interface EventDetail {
  id: number;
  name: string;
  waves: { id: number; name: string; gun_time: string }[];
  categories: {
    id: number; name: string; laps_required: number;
    mixed: boolean; rank_by: string;
  }[];
  courses: {
    id: number; name: string;
    nodes: {
      order_index: number; code: string; name: string; kind: string;
      min_split_s: number | null; max_split_s: number | null;
    }[];
  }[];
  competitors: {
    id: number; bib: string; name: string; category_id: number;
    wave_id: number; course_id: number; initial_chip: string;
    class_label: string | null;
  }[];
}

export interface AdjudicationRow {
  id: number;
  issue_key: string;
  kind: string;
  kind_title: string;
  competitor_id: number | null;
  decision: string;
  decision_label: string;
  reason: string;
  decided_by: string;
  decided_at: string;
  payload: any;
}
