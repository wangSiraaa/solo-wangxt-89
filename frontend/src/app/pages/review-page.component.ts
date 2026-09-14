import { Component, Input, OnChanges, computed } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { EventState } from '../event.state';
import { IssueCardComponent } from '../issue-card.component';
import { STATUS_LABELS } from '../api.service';

type Filter = 'open' | 'all' | 'resolved';

@Component({
  standalone: true,
  imports: [FormsModule, IssueCardComponent],
  template: `
    <div class="row" style="margin-bottom:14px">
      <div class="stat">
        <div class="n">{{ results().length }}</div>
        <div class="l">选手</div>
      </div>
      <div class="stat">
        <div class="n" style="color:var(--bad)">{{ open().length }}</div>
        <div class="l">待核实疑点</div>
      </div>
      <div class="stat">
        <div class="n" style="color:var(--ok)">{{ resolved().length }}</div>
        <div class="l">已裁定</div>
      </div>
      <div class="stat">
        <div class="n" style="color:var(--warn)">{{ state.recalibrationPending().length }}</div>
        <div class="l">校时后待重核验</div>
      </div>
      <div class="stat">
        <div class="n">{{ finishers().length }}</div>
        <div class="l">无异议完赛</div>
      </div>
      <span style="flex:1"></span>
      <select [(ngModel)]="filter">
        <option value="open">仅待核实</option>
        <option value="all">全部疑点</option>
        <option value="resolved">仅已裁定</option>
      </select>
      <button class="secondary" (click)="state.refresh()">重新重放</button>
    </div>

    @for (issue of visible(); track issue.issue_key) {
      <app-issue-card [issue]="issue" />
    } @empty {
      <div class="panel">
        <strong>没有待核实疑点。</strong>
        <span class="muted">所有读卡均已处置，可前往「榜单 / 发布」发布成绩。</span>
      </div>
    }

    <div class="panel">
      <h2>选手状态总览（候选结果由重放生成）</h2>
      <table>
        <thead>
          <tr>
            <th>号码布</th><th>姓名</th><th>组别</th><th>状态</th>
            <th>确认圈数</th><th>净计时</th><th>枪声计时</th><th>未决疑点</th>
          </tr>
        </thead>
        <tbody>
          @for (r of results(); track r.competitor_id) {
            <tr>
              <td>{{ r.bib }}</td>
              <td>{{ r.name }}</td>
              <td>{{ r.category_name }}
                @if (r.class_label) { <span class="muted">({{ r.class_label }})</span> }
              </td>
              <td>
                <span class="badge {{ r.status }}">{{ STATUS_LABELS[r.status] }}</span>
              </td>
              <td>{{ r.confirmed_laps }} / {{ r.laps_required }}</td>
              <td class="mono">{{ r.total_net_s !== null ? r.total_net_s + ' s' : '—' }}</td>
              <td class="mono">{{ r.total_gun_s !== null ? r.total_gun_s + ' s' : '—' }}</td>
              <td>
                @for (k of r.open_issue_keys; track k) {
                  <div class="issue-key">{{ k }}</div>
                }
              </td>
            </tr>
          }
        </tbody>
      </table>
    </div>
  `,
})
export class ReviewPageComponent implements OnChanges {
  @Input() id!: string;

  filter: Filter = 'open';
  STATUS_LABELS = STATUS_LABELS;

  constructor(public state: EventState) {}

  ngOnChanges(): void {
    this.state.syncTo(Number(this.id));
  }

  readonly open = computed(
    () => this.state.replay()?.output.issues.filter((i) => !i.resolution) ?? [],
  );
  readonly resolved = computed(
    () => this.state.replay()?.output.issues.filter((i) => i.resolution) ?? [],
  );
  readonly finishers = computed(
    () => this.state.replay()?.output.results.filter(
      (r) => r.status === 'FINISHER') ?? [],
  );

  results = computed(() => this.state.replay()?.output.results ?? []);

  readonly visible = computed(() => {
    const all = this.state.replay()?.output.issues ?? [];
    if (this.filter === 'all') return all;
    return all.filter((i) =>
      this.filter === 'open' ? !i.resolution : i.resolution);
  });
}
