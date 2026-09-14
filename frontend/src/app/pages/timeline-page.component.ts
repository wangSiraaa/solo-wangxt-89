import { Component, Input, effect, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { EventState } from '../event.state';
import { AthleteTimelineComponent } from '../athlete-timeline.component';
import { STATUS_LABELS } from '../api.service';

@Component({
  standalone: true,
  imports: [FormsModule, AthleteTimelineComponent],
  template: `
    <div class="row" style="margin-bottom:12px">
      <select [(ngModel)]="selectedBib" style="min-width:220px">
        @for (r of results(); track r.competitor_id) {
          <option [value]="r.bib">
            {{ r.bib }} {{ r.name }} — {{ STATUS_LABELS[r.status] }}
            （{{ r.confirmed_laps }}/{{ r.laps_required }} 圈）
          </option>
        }
      </select>
      <input placeholder="按号码布/姓名过滤…" [(ngModel)]="q">
      <span class="muted">绿=有效 橙=挂起/待核实 红=剔除 灰=重复折叠（点击圆点看疑点编号）</span>
    </div>

    @if (selected()) {
      <app-athlete-timeline [result]="selected()!" />
    }
  `,
})
export class TimelinePageComponent {
  @Input() id!: string;

  selectedBib = '';
  q = '';
  STATUS_LABELS = STATUS_LABELS;

  constructor(public state: EventState) {
    effect(() => {
      if (this.state.eventId() !== Number(this.id)) {
        this.state.open(Number(this.id));
      }
      if (!this.selectedBib && this.state.replay()) {
        const first = this.state.replay()!.output.results[0];
        if (first) this.selectedBib = first.bib;
      }
    });
  }

  selected = () => {
    const results = this.state.replay()?.output.results ?? [];
    const q = this.q.trim();
    return results.find((r) => {
      if (q) {
        return r.bib.includes(q) || r.name.includes(q);
      }
      return r.bib === this.selectedBib;
    });
  };

  results = () => this.state.replay()?.output.results ?? [];
}
