import { Component, Input, effect, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { DatePipe } from '@angular/common';
import { lastValueFrom } from 'rxjs';
import { EventState } from '../event.state';
import { ApiService, STATUS_LABELS, fmtDuration } from '../api.service';

@Component({
  standalone: true,
  imports: [FormsModule, DatePipe],
  template: `
    <div class="panel">
      <h2>发布成绩
        <span class="muted" style="font-weight:400">
          成绩由原始读卡+裁定重放生成；有未决疑点时禁止发布，且不存在手工改最终时间的入口。
        </span>
      </h2>
      <div class="row">
        <input [(ngModel)]="decidedBy" placeholder="发布人" size="10">
        <button (click)="publish()" [disabled]="publishing()">
          {{ publishing() ? '发布中…' : '重新重放并发布' }}
        </button>
        <button class="secondary" (click)="loadPublished()">查看已发布版本</button>
        @if (state.openIssues().length > 0) {
          <span class="badge open">尚有 {{ state.openIssues().length }} 项未确认</span>
        } @else {
          <span class="badge resolved">全部疑点已确认</span>
        }
      </div>
      @if (published(); as pub) {
        <h3>已发布版本 v{{ pub.version }}
          <span class="muted mono" style="font-weight:400">
            输入哈希 {{ pub.input_hash?.slice(0, 16) }}… ·
            {{ pub.published_at | date:'yyyy-MM-dd HH:mm' }}
          </span>
        </h3>
      }
    </div>

    @for (board of boards(); track board.category_id) {
      <div class="panel">
        <h2>{{ category(board.category_id)?.name }}
          <span class="muted" style="font-weight:400">
            {{ category(board.category_id)?.laps_required }} 圈 ·
            排名依据：{{ category(board.category_id)?.rank_by === 'net' ? '净计时' : '枪声计时' }}
            @if (category(board.category_id)?.mixed) { · 混合组别（含子组名次） }
          </span>
        </h2>
        <table>
          <thead>
            <tr>
              <th>总名次</th>
              @if (category(board.category_id)?.mixed) { <th>子组名次</th> }
              <th>号码布</th><th>姓名</th><th>子组</th>
              <th>圈数</th><th>净计时</th><th>枪声计时</th><th>状态</th>
            </tr>
          </thead>
          <tbody>
            @for (e of board.entries; track e.competitor_id) {
              <tr [class.muted]="e.rank === null">
                <td>{{ e.rank ?? '—' }}</td>
                @if (category(board.category_id)?.mixed) { <td>{{ e.class_rank ?? '—' }}</td> }
                <td>{{ e.bib }}</td>
                <td>{{ e.name }}</td>
                <td>{{ e.class_label ?? '' }}</td>
                <td>{{ e.laps }}</td>
                <td class="mono">{{ fmtDuration(e.total_net_s) }}</td>
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
export class LeaderboardPageComponent {
  @Input() id!: string;

  decidedBy = 'chief';
  publishing = signal(false);
  published = signal<any>(null);
  fmtDuration = fmtDuration;
  STATUS_LABELS = STATUS_LABELS;

  boards = () => this.state.replay()?.output.leaderboard ?? [];

  constructor(public state: EventState, private api: ApiService) {
    effect(() => {
      if (this.state.eventId() !== Number(this.id)) {
        this.state.open(Number(this.id));
      }
    });
  }

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
        alert(detail.message + '\n\n' +
          detail.open_issues
            .map((i: any) => `· [${i.title}] ${i.issue_key}`)
            .join('\n'));
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
}
