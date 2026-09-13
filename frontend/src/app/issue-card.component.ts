import {
  Component, EventEmitter, Input, Output, signal,
} from '@angular/core';
import { FormsModule } from '@angular/forms';
import { JsonPipe, DatePipe } from '@angular/common';
import { Issue } from './models';
import { EventState } from './event.state';

@Component({
  selector: 'app-issue-card',
  standalone: true,
  imports: [FormsModule, JsonPipe, DatePipe],
  template: `
    <div class="issue-card" [class.resolved]="!!issue.resolution">
      <div class="issue-head">
        <span class="badge kind">{{ issue.kind }}</span>
        <strong>{{ issue.title }}</strong>
        <span class="issue-key">{{ issue.issue_key }}</span>
        <span class="badge" [class.open]="!issue.resolution"
              [class.resolved]="!!issue.resolution">
          {{ issue.resolution ? '已裁定' : '待核实' }}
        </span>
      </div>

      <div class="muted">
        关联选手：<strong>{{ state.competitorName(issue.competitor_id) }}</strong>
      </div>

      <!-- 判定依据：原始证据明细 -->
      <div class="detail">判定依据（系统记录）
{{ detailJson }}</div>

      <!-- 已有裁定：历史可查，仍可重新决定（追加新裁定，不覆盖旧记录） -->
      @if (issue.resolution) {
        <div class="resolution-box">
          <div><strong>{{ issue.resolution.decision_label }}</strong>
            · {{ issue.resolution.decided_by }}
            · {{ issue.resolution.decided_at | date:'yyyy-MM-dd HH:mm:ss' }}
          </div>
          <div>理由：{{ issue.resolution.reason }}</div>
          @if (issue.resolution.payload &&
               (issue.resolution.payload | json) !== '{}') {
            <div class="mono">附加：{{ issue.resolution.payload | json }}</div>
          }
        </div>
      }

      <details [open]="!issue.resolution" style="margin-top:8px">
        <summary class="muted" style="cursor:pointer;font-size:12px">
          {{ issue.resolution ? '重新裁定（追加记录，不覆盖原决定）' : '逐项确认' }}
        </summary>
        <div class="adjudicate-row">
          <select [(ngModel)]="chosen">
            @for (opt of issue.options; track opt) {
              <option [value]="opt">{{ optionLabel(opt) }}</option>
            }
          </select>
          <input placeholder="裁定人" [(ngModel)]="decidedBy" size="8">
          @if (chosen === 'ATTACH_CHIP' && issue.competitor_id === null) {
            <select [(ngModel)]="competitorId">
              <option [ngValue]="undefined">挂接到选手…</option>
              @for (c of state.detail()?.competitors ?? []; track c.id) {
                <option [ngValue]="c.id">{{ c.bib }} {{ c.name }}</option>
              }
            </select>
            <input type="datetime-local" [(ngModel)]="validFrom">
          }
          <textarea #reason placeholder="裁定理由（必填，至少 4 字）"
                    style="flex:1 1 220px"></textarea>
          <button [disabled]="busy()"
                  (click)="submit(reason.value)">
            {{ busy() ? '提交中…' : (issue.resolution ? '追加新裁定' : '确认此项') }}
          </button>
        </div>
      </details>
    </div>
  `,
})
export class IssueCardComponent {
  @Input() issue!: Issue;
  @Output() decided = new EventEmitter<void>();

  readonly busy = signal(false);
  chosen = '';
  decidedBy = 'official';
  competitorId: number | undefined;
  validFrom = '';

  constructor(public state: EventState) {}

  ngOnInit(): void {
    this.chosen = this.issue.options[0] ?? '';
  }

  get detailJson(): string {
    return JSON.stringify(this.issue.detail, null, 2);
  }

  optionLabel(opt: string): string {
    const labels: Record<string, string> = {
      KEEP_FIRST: '采用最早读卡',
      KEEP_LATEST: '采用最晚读卡',
      CREDIT_NODE: '认可漏点（确认经过）',
      DISALLOW_LAP: '该圈不予计圈',
      ACCEPT_READ: '接受该读卡',
      REJECT_READ: '剔除该读卡',
      CREDIT_LAP: '认可完成一圈',
      CONFIRM_READ: '确认读卡有效',
      ATTACH_CHIP: '挂接到选手（换芯片）',
      REJECT_CHIP: '芯片不属于本场',
    };
    return labels[opt] ?? opt;
  }

  async submit(reason: string): Promise<void> {
    if (reason.trim().length < 4) {
      alert('请填写裁定理由（至少 4 字）');
      return;
    }
    if (this.chosen === 'ATTACH_CHIP' &&
        this.issue.competitor_id === null &&
        this.competitorId === undefined) {
      alert('挂接芯片必须选择选手');
      return;
    }
    this.busy.set(true);
    try {
      await this.state.decide(
        this.issue.issue_key,
        this.chosen,
        reason.trim(),
        this.decidedBy || 'official',
        this.competitorId,
        this.validFrom ? new Date(this.validFrom).toISOString() : undefined,
      );
      this.decided.emit();
    } catch (e: any) {
      alert(e?.error?.detail ?? e?.message ?? String(e));
    } finally {
      this.busy.set(false);
    }
  }
}
