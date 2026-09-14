import { Component, Input, effect, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { DatePipe } from '@angular/common';
import { lastValueFrom } from 'rxjs';
import { EventState } from '../event.state';
import { ApiService } from '../api.service';

@Component({
  standalone: true,
  imports: [FormsModule, DatePipe],
  template: `
    <div class="panel">
      <h2>设备时钟对时（只追加的技术校时层）
        <span class="muted" style="font-weight:400;font-size:12px">
          对时只改变派生时间；原始读卡的钟面时间与服务器接收时间永久保留。
          重启后设备钟回跳时，按接收时刻分段。
        </span>
      </h2>
      <div class="adjudicate-row">
        <input [(ngModel)]="device" placeholder="设备号 如 FDEV" size="10">
        <label>设备钟面
          <input type="datetime-local" step="1" [(ngModel)]="deviceTime">
        </label>
        <label>真实时刻
          <input type="datetime-local" step="1" [(ngModel)]="trueTime">
        </label>
        <input [(ngModel)]="epsilon" type="number" step="0.1" placeholder="参考误差(s)" size="5">
        <input [(ngModel)]="note" placeholder="依据（NTP/点位对表…）" style="flex:1">
        <input [(ngModel)]="recordedBy" placeholder="记录人" size="8">
        <button (click)="add()" [disabled]="busy()">添加对时</button>
      </div>
      @if (error()) { <div class="toast-error">{{ error() }}</div> }
    </div>

    <div class="panel">
      <h2>可信对时事件</h2>
      <table>
        <thead>
          <tr>
            <th>#</th><th>设备</th><th>设备钟面</th><th>真实时刻</th>
            <th>参考误差(s)</th><th>服务器接收</th><th>来源</th><th>记录人</th>
          </tr>
        </thead>
        <tbody>
          @for (s of state.clockSyncs(); track s.id) {
            <tr>
              <td>{{ s.id }}</td>
              <td class="mono">{{ s.device_id }}</td>
              <td class="mono">{{ s.device_time | date:'yyyy-MM-dd HH:mm:ss' }}</td>
              <td class="mono">{{ s.true_time | date:'yyyy-MM-dd HH:mm:ss' }}</td>
              <td>{{ s.reference_epsilon_s }}</td>
              <td class="mono muted">
                {{ s.received_at | date:'HH:mm:ss' }}
              </td>
              <td>{{ s.source }} {{ s.note ? '· ' + s.note : '' }}</td>
              <td>{{ s.recorded_by }}</td>
            </tr>
          } @empty {
            <tr><td colspan="8" class="muted">暂无对时事件（未校准设备按钟面原值处理）。</td></tr>
          }
        </tbody>
      </table>
    </div>
  `,
})
export class ClockPageComponent {
  @Input() id!: string;

  device = 'FDEV';
  deviceTime = '';
  trueTime = '';
  epsilon = 0;
  note = '';
  recordedBy = 'timekeeper';
  busy = signal(false);
  error = signal('');

  constructor(public state: EventState, private api: ApiService) {
    effect(() => {
      if (this.state.eventId() !== Number(this.id)) {
        this.state.open(Number(this.id));
      }
    });
  }

  async add(): Promise<void> {
    if (!this.device || !this.deviceTime || !this.trueTime) {
      this.error.set('设备号、设备钟面、真实时刻均必填');
      return;
    }
    this.busy.set(true);
    this.error.set('');
    try {
      const key = `${this.device}-${this.deviceTime}-${Date.now()}`;
      const res = await lastValueFrom(this.api.addClockSyncs(Number(this.id), [{
        device_id: this.device,
        device_time: new Date(this.deviceTime).toISOString(),
        true_time: new Date(this.trueTime).toISOString(),
        reference_epsilon_s: Number(this.epsilon) || 0,
        source: 'manual',
        note: this.note || null,
        recorded_by: this.recordedBy,
        idempotency_key: key,
      }]));
      if (res.context_changed_issues?.length) {
        alert(`对时已记录，以下旧裁定需重新核验（原决定保留）：\n`
          + res.context_changed_issues.join('\n'));
      }
      await this.state.refresh();
      this.note = '';
    } catch (e: any) {
      this.error.set(e?.error?.detail ?? e?.message ?? String(e));
    } finally {
      this.busy.set(false);
    }
  }
}
