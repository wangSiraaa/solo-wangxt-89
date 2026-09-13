import { Injectable, computed, signal } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { lastValueFrom } from 'rxjs';
import {
  AdjudicationRow, EventDetail, Issue, ReplayEnvelope, ResultRow,
} from './models';
import { ApiService } from './api.service';

/** Shared state for the currently opened event. */
@Injectable({ providedIn: 'root' })
export class EventState {
  readonly eventId = signal<number | null>(null);
  readonly detail = signal<EventDetail | null>(null);
  readonly replay = signal<ReplayEnvelope | null>(null);
  readonly adjudications = signal<AdjudicationRow[]>([]);
  readonly loading = signal(false);
  readonly error = signal('');

  readonly inputHash = computed(() => this.replay()?.input_hash ?? '');
  readonly outputHash = computed(() => this.replay()?.output_hash ?? '');
  readonly openIssues = computed<Issue[]>(
    () => this.replay()?.output.issues.filter((i) => !i.resolution) ?? [],
  );

  constructor(
    private http: HttpClient,
    private api: ApiService,
  ) {}

  competitorName(id: number | null): string {
    if (id === null) return '未归属';
    const c = this.detail()?.competitors.find((x) => x.id === id);
    return c ? `${c.bib} ${c.name}` : `#${id}`;
  }

  resultOf(issue: Issue): ResultRow | undefined {
    return this.replay()?.output.results.find(
      (r) => r.competitor_id === issue.competitor_id,
    );
  }

  async open(eventId: number): Promise<void> {
    this.eventId.set(eventId);
    await this.refresh();
  }

  async refresh(): Promise<void> {
    const id = this.eventId();
    if (id === null) return;
    this.loading.set(true);
    this.error.set('');
    try {
      const [detail, envelope, hist] = await Promise.all([
        lastValueFrom(this.api.eventDetail(id)),
        lastValueFrom(this.api.replay(id)),
        lastValueFrom(this.api.adjudications(id)),
      ]);
      this.detail.set(detail);
      this.replay.set(envelope);
      this.adjudications.set(hist);
    } catch (e: any) {
      this.error.set(
        e?.error?.detail?.message
          ? e.error.detail.message +
            (e.error.detail.open_issues
              ? '\n未决疑点 ' + e.error.detail.open_issues.length + ' 项'
              : '')
          : String(e?.message ?? e),
      );
    } finally {
      this.loading.set(false);
    }
  }

  async decide(
    issueKey: string,
    decision: string,
    reason: string,
    decidedBy: string,
    competitorId?: number,
    validFrom?: string,
  ): Promise<void> {
    const id = this.eventId();
    if (id === null) return;
    const body: any = {
      issue_key: issueKey,
      decision,
      reason,
      decided_by: decidedBy,
    };
    if (competitorId !== undefined) body.competitor_id = competitorId;
    if (validFrom) body.valid_from = validFrom;
    await lastValueFrom(this.api.decide(id, issueKey, body));
    await this.refresh();
  }

  clearError(): void {
    this.error.set('');
  }
}
