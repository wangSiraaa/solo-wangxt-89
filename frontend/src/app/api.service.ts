import { Injectable, signal } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import {
  AdjudicationRow, EventDetail, EventSummary, ReplayEnvelope,
} from './models';

@Injectable({ providedIn: 'root' })
export class ApiService {
  readonly inputHash = signal<string>('');
  readonly outputHash = signal<string>('');
  readonly error = signal<string>('');

  constructor(private http: HttpClient) {}

  listEvents(): Observable<EventSummary[]> {
    return this.http.get<EventSummary[]>('/api/events');
  }

  eventDetail(id: number): Observable<EventDetail> {
    return this.http.get<EventDetail>(`/api/events/${id}`);
  }

  replay(id: number): Observable<ReplayEnvelope> {
    return this.http.post<ReplayEnvelope>(`/api/events/${id}/replay`, {});
  }

  latestReplay(id: number): Observable<ReplayEnvelope | any> {
    return this.http.get(`/api/events/${id}/replay/latest`);
  }

  decide(
    eventId: number,
    issueKey: string,
    body: {
      issue_key: string;
      decision: string;
      reason: string;
      decided_by: string;
      competitor_id?: number;
      valid_from?: string;
    },
  ): Observable<{ adjudication_id: number }> {
    return this.http.post<{ adjudication_id: number }>(
      `/api/events/${eventId}/issues/${encodeURIComponent(issueKey)}/adjudications`,
      body,
    );
  }

  adjudications(eventId: number): Observable<AdjudicationRow[]> {
    return this.http.get<AdjudicationRow[]>(
      `/api/events/${eventId}/adjudications`,
    );
  }

  publish(eventId: number, decidedBy: string): Observable<any> {
    return this.http.post(`/api/events/${eventId}/publish`, {
      decided_by: decidedBy,
    });
  }

  published(eventId: number): Observable<any> {
    return this.http.get(`/api/events/${eventId}/results/published`);
  }
}

/** mm:ss or hh:mm:ss formatting for UI display. */
export function fmtDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return '—';
  const sign = seconds < 0 ? '-' : '';
  seconds = Math.abs(seconds);
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.round(seconds % 60);
  const mm = String(m).padStart(2, '0');
  const ss = String(s).padStart(2, '0');
  return sign + (h > 0 ? `${h}:${mm}:${ss}` : `${m}:${ss}`);
}

export function fmtClock(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleTimeString('zh-CN', { hour12: false }) +
    '.' + String(d.getMilliseconds()).padStart(3, '0');
}

export const STATUS_LABELS: Record<string, string> = {
  FINISHER: '正常完赛',
  FINISHER_PENDING_REVIEW: '完赛·待核实',
  IN_REVIEW: '待核实',
  DNF: '未完赛',
  DQ: '取消成绩',
};

export const TREATMENT_LABELS: Record<string, string> = {
  accepted: '有效',
  held_review: '挂起待核实',
  rejected: '已剔除',
  duplicate_dropped: '重复读卡（折叠）',
  unknown_node_dropped: '未知节点（剔除）',
};
