import { Component, Input, effect } from '@angular/core';
import { DatePipe, JsonPipe } from '@angular/common';
import { EventState } from '../event.state';

@Component({
  standalone: true,
  imports: [DatePipe, JsonPipe],
  template: `
    <div class="panel">
      <h2>人工裁定历史
        <span class="muted" style="font-weight:400">
          只追加、不覆盖；同一疑点可多次裁定，重放以最新一条为准，旧记录始终保留。
        </span>
      </h2>
      <table>
        <thead>
          <tr>
            <th>#</th><th>时间</th><th>裁定人</th><th>选手</th>
            <th>疑点类型</th><th>疑点编号</th><th>决定</th><th>理由</th>
            <th>附加载荷</th>
          </tr>
        </thead>
        <tbody>
          @for (a of state.adjudications(); track a.id) {
            <tr>
              <td>{{ a.id }}</td>
              <td class="mono" style="font-size:11px">
                {{ a.decided_at | date:'yyyy-MM-dd HH:mm:ss' }}
              </td>
              <td>{{ a.decided_by }}</td>
              <td>{{ state.competitorName(a.competitor_id) }}</td>
              <td>{{ a.kind_title }}</td>
              <td class="issue-key">{{ a.issue_key }}</td>
              <td><strong>{{ a.decision_label }}</strong></td>
              <td>{{ a.reason }}</td>
              <td class="detail" style="margin:0">{{ a.payload | json }}</td>
            </tr>
          } @empty {
            <tr><td colspan="9" class="muted">暂无人工裁定。</td></tr>
          }
        </tbody>
      </table>
    </div>
  `,
})
export class HistoryPageComponent {
  @Input() id!: string;

  constructor(public state: EventState) {
    effect(() => {
      if (this.state.eventId() !== Number(this.id)) {
        this.state.open(Number(this.id));
      }
    });
  }
}
