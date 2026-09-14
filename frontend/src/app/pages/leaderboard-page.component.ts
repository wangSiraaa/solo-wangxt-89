import { Component, Input, OnChanges, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { lastValueFrom } from 'rxjs';
import { EventState } from '../event.state';
import { ApiService, STATUS_LABELS, fmtDuration } from '../api.service';
import { CompareRow } from '../models';

@Component({
  standalone: true,
  imports: [FormsModule],
  template: `
    <div class="panel">
      <h2>发布成绩
        <span class="muted" style="font-weight:400">
          成绩由原始读卡+对时+裁定重放生成；有未决疑点或校时后待重核验时禁止发布。
        </span>
      </h2>
      <div class="row">
        <input [(ngModel)]="decidedBy" placeholder="发布人" size="10">
        <button (click)="publish()" [disabled]="publishing()">
          {{ publishing() ? '发布中…' : '重新重放并发布' }}
        </button>
        <button class="secondary" (click)="loadPublished()">刷新已发布</button>
        <button class="secondary" (click)="loadCompare()">对比旧榜/修订榜</button>
        @if (state.openIssues().length > 0) {
          <span class="badge open">未决 {{ state.openIssues().length }}</span>
        }
        @if (state.recalibrationPending().length > 0) {
          <span class="badge tie">校时待重核验 {{ state.recalibrationPending().length }}</span>
        }
        @if (!state.openIssues().length && !state.recalibrationPending().length) {
          <span class="badge resolved">可发布</span>
        }
      </div>
      <div class="muted" style="margin-top:8px;font-size:12px">
        分层哈希：时钟 <span class="mono">{{ state.clockHash() }}</span> ·
        身份绑定 <span class="mono">{{ state.identityHash() }}</span> ·
        人工裁定 <span class="mono">{{ state.decisionsHash() }}</span>
      </div>
      @if (published(); as pub) {
        <h3>已发布版本 v{{ pub.version }}
          <span class="muted mono" style="font-weight:400">
            输入哈希 {{ pub.input_hash?.slice(0, 16) }}…
          </span>
        </h3>
      }
    </div>

    @if (compare(); as cmp) {
      <div class="panel">
        <h2>旧榜 v{{ cmp.old_version ?? '—' }} → 修订榜对比
          <span class="muted" style="font-weight:400;font-size:12px">
            依据输入哈希 {{ cmp.input_hash.slice(0, 16) }}…
          </span>
        </h2>
        <table>
          <thead>
            <tr>
              <th>号码布</th><th>姓名</th><th>旧名次</th><th>新名次</th>
              <th>变化</th><th>旧成绩(s)</th><th>新成绩点估计(s)</th>
              <th>不确定区间(s)</th><th>裁定</th>
            </tr>
          </thead>
          <tbody>
            @for (r of cmp.rows; track r.competitor_id) {
              <tr>
                <td>{{ r.bib }}</td><td>{{ r.name }}</td>
                <td>{{ r.old_rank ?? '—' }}</td>
                <td>{{ r.new_rank ?? '—' }}</td>
                <td>
                  @if (r.rank_delta === null) { <span class="muted">—</span> }
                  @else if (r.rank_delta > 0) {
                    <span class="rank-delta-up">▲{{ r.rank_delta }}</span>
                  } @else if (r.rank_delta < 0) {
                    <span class="rank-delta-down">▼{{ -r.rank_delta }}</span>
                  } @else { <span class="muted">持平</span> }
                </td>
                <td class="mono">{{ r.old_total_s ?? '—' }}</td>
                <td class="mono">{{ r.new_total_s ?? '—' }}</td>
                <td class="interval">
                  @if (r.new_total_lo !== null) {
                    [{{ fmtDuration(r.new_total_lo) }} ~ {{ fmtDuration(r.new_total_hi) }}]
                  } @else { — }
                </td>
                <td>
                  @if (r.tie_label) {
                    <span class="badge tie">{{ r.tie_label }}</span>
                  }
                </td>
              </tr>
            }
          </tbody>
        </table>
      </div>
    }

    @for (board of boards(); track board.category_id) {
      <div class="panel">
        @if (category(board.category_id); as cat) {
          <h2>{{ cat.name }}
            <span class="muted" style="font-weight:400">
              {{ cat.laps_required }} 圈 ·
              排名依据：{{ cat.rank_by === 'net' ? '净计时' : '枪声计时' }}
              @if (cat.mixed) { · 混合组别（含子组名次） }
            </span>
          </h2>
        }
        <table>
          <thead>
            <tr>
              <th>总名次</th>
              @if (category(board.category_id)?.mixed) { <th>子组名次</th> }
              <th>号码布</th><th>姓名</th><th>子组</th>
              <th>圈数</th><th>净计时(点估计)</th><th>不确定区间</th>
              <th>枪声计时</th><th>状态</th>
            </tr>
          </thead>
          <tbody>
            @for (e of board.entries; track e.competitor_id) {
              <tr [class.muted]="e.rank === null">
                <td>
                  {{ e.rank ?? '—' }}
                  @if (e.tie_label) { <span class="badge tie">{{ e.tie_label }}</span> }
                </td>
                @if (category(board.category_id)?.mixed) {
                  <td>
                    {{ e.class_rank ?? '—' }}
                    @if (e.class_tie_label) {
                      <span class="badge tie">{{ e.class_tie_label }}</span>
                    }
                  </td>
                }
                <td>{{ e.bib }}</td>
                <td>{{ e.name }}</td>
                <td>{{ e.class_label ?? '' }}</td>
                <td>{{ e.laps }}</td>
                <td class="mono">{{ fmtDuration(e.total_net_s) }}</td>
                <td class="interval">
                  @if (e.finish_uncertain) {
                    [{{ fmtDuration(e.total_net_lo) }} ~ {{ fmtDuration(e.total_net_hi) }}]
                  } @else { — }
                </td>
                <td class="mono">{{ fmtDuration(e.total_gun_s) }}</td>
                <td><span class="badge {{ e.status }}">{{ STATUS_LABELS[e.status] ?? e.status }}</span></td>
              </tr>
            }
          </tbody>
        </table>
      </div>
    }
  `,
})
export class LeaderboardPageComponent implements OnChanges {
  @Input() id!: string;

  decidedBy = 'chief';
  publishing = signal(false);
  published = signal<any>(null);
  compare = signal<{
    old_version: number | null; input_hash: string; rows: CompareRow[];
  } | null>(null);
  fmtDuration = fmtDuration;
  STATUS_LABELS = STATUS_LABELS;

  constructor(public state: EventState, private api: ApiService) {}

  ngOnChanges(): void {
    this.state.syncTo(Number(this.id));
  }

  boards = () => this.state.replay()?.output.leaderboard ?? [];

  category(catId: number) {
    return this.state.detail()?.categories.find((c) => c.id === catId);
  }

  async publish(): Promise<void> {
    this.publishing.set(true);
    this.state.clearError();
    try {
      await lastValueFrom(this.api.publish(Number(this.id), this.decidedBy));
      await this.state.refresh();
      await this.loadPublished();
    } catch (e: any) {
      const detail = e?.error?.detail;
      if (detail?.message) {
        const open = (detail.open_issues ?? [])
          .map((i: any) => `· 未决 [${i.title}] ${i.issue_key}`).join('\n');
        const recal = (detail.recalibration_pending ?? [])
          .map((i: any) => `· 校时待重核验 [${i.issue_key}] 设备 ${i.devices.join(',')}`)
          .join('\n');
        alert([detail.message, open, recal].filter(Boolean).join('\n'));
      } else {
        alert(detail ?? e?.message ?? String(e));
      }
    } finally {
      this.publishing.set(false);
    }
  }

  async loadPublished(): Promise<void> {
    this.published.set(
      await lastValueFrom(this.api.published(Number(this.id))));
  }

  async loadCompare(): Promise<void> {
    const c = await lastValueFrom(this.api.compare(Number(this.id)));
    this.compare.set(c);
  }
}
