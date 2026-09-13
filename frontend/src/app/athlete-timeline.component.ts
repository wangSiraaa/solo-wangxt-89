import { Component, Input, computed, signal } from '@angular/core';
import { DatePipe } from '@angular/common';
import { ResultRow } from './models';
import { fmtClock, fmtDuration, STATUS_LABELS, TREATMENT_LABELS } from './api.service';

interface Dot {
  x: number;
  cls: string;
  code: string;
  time: string;
      chip: string;
  treatment: string;
  issueKey: string | null;
}

@Component({
  selector: 'app-athlete-timeline',
  standalone: true,
  imports: [DatePipe],
  template: `
    <div class="panel">
      <h2>
        <span class="badge {{ result.status }}">
          {{ statusLabel(result.status) }}
        </span>
        {{ result.bib }} {{ result.name }}
        <span class="muted" style="font-weight:400">
          {{ result.category_name }}
          @if (result.class_label) { · {{ result.class_label }} }
          · 需 {{ result.laps_required }} 圈 · 已确认 {{ result.confirmed_laps }} 圈
          @if (result.extra_laps > 0) { · 多跑 {{ result.extra_laps }} 圈 }
        </span>
      </h2>

      <div class="row" style="margin-bottom:10px">
        <div class="stat">
          <div class="n">{{ fmtDuration(result.total_net_s) }}</div>
          <div class="l">净计时（首次起点毯 {{ netBasis }}）</div>
        </div>
        <div class="stat">
          <div class="n">{{ fmtDuration(result.total_gun_s) }}</div>
          <div class="l">枪声计时（{{ result.gun_time | date:'HH:mm:ss' }} 起跑）</div>
        </div>
        @for (sw of result.chip_switches; track sw.at) {
          <div class="stat switch-note">
            换芯片：{{ sw.from_chip }} → {{ sw.to_chip }}
            <div class="l">{{ sw.at | date:'HH:mm:ss' }}</div>
          </div>
        }
      </div>

      <!-- horizontal swim lanes, one per lap -->
      <div class="timeline-wrap">
        <div class="tl-track" [style.width.px]="trackWidth()">
          @for (tick of ticks(); track tick.label) {
            <div class="tl-tick" [style.left.px]="tick.x">
              <div class="tl-tick-label">{{ tick.label }}</div>
            </div>
          }
          @for (lap of lanes(); track lap.label) {
            <div class="tl-lane">
              <div class="tl-lane-label">{{ lap.label }}</div>
              @for (d of lap.dots; track d.time + d.code) {
                <div class="tl-dot {{ d.cls }}"
                     [style.left.px]="d.x"
                     [title]="d.code + ' ' + d.time + '\n' +
                              treatment(d.treatment) +
                              (d.issueKey ? '\n疑点 ' + d.issueKey : '') +
                              '\n芯片 ' + d.chip">
                  <div class="tl-dot-label">{{ d.code }}</div>
                </div>
              }
            </div>
          }
        </div>
      </div>

      <!-- candidate lap detail -->
      <h3>候选圈次（原始读卡重放生成，不手工改时间）</h3>
      <table>
        <thead>
          <tr>
            <th>圈</th><th>状态</th><th>起点依据</th><th>起</th><th>终</th>
            <th>圈用时(净)</th><th>枪声累计</th><th>节点链</th><th>漏点</th><th>关联疑点</th>
          </tr>
        </thead>
        <tbody>
          @for (lap of result.laps; track lap.lap_no) {
            <tr>
              <td>{{ lap.lap_no }}</td>
              <td><span class="badge {{ lap.status }}">{{ lapStatus(lap.status) }}</span></td>
              <td class="muted" style="font-size:11px">{{ lap.start_basis }}</td>
              <td class="mono">{{ fmtClock(lap.start_time) }}</td>
              <td class="mono">{{ fmtClock(lap.finish_time) }}</td>
              <td class="mono">{{ fmtDuration(lap.net_s) }}</td>
              <td class="mono">{{ fmtDuration(lap.gun_elapsed_s) }}</td>
              <td class="mono" style="font-size:11px">
                @for (s of lap.segments; track s.node_code + s.time) {
                  <span [class.muted]="!s.feasible">{{ s.node_code }}</span>
                  @if (!$last) { → }
                }
              </td>
              <td>
                @for (m of lap.missing_nodes; track m) {
                  <span class="badge kind">{{ m }}</span>
                }
              </td>
              <td class="mono" style="font-size:10px">
                @for (k of lap.issue_keys; track k) {
                  <div>{{ k }}</div>
                }
              </td>
            </tr>
          }
          @if (result.partial_lap) {
            <tr>
              <td>{{ result.partial_lap.lap_no }}+</td>
              <td><span class="badge in_progress">进行中</span></td>
              <td colspan="8" class="muted">
                已见节点：
                @for (s of result.partial_lap.segments; track s.node_code + s.time) {
                  {{ s.node_code }}
                }
              </td>
            </tr>
          }
        </tbody>
      </table>

      <!-- every raw read and how it was treated (evidence stays visible) -->
      <h3>原始读卡处置明细</h3>
      <table>
        <thead>
          <tr><th>时间</th><th>设备</th><th>序号</th><th>芯片</th><th>节点</th><th>处置</th><th>疑点</th></tr>
        </thead>
        <tbody>
          @for (ev of result.timeline; track ev.read_id) {
            <tr>
              <td class="mono">{{ fmtClock(ev.time) }}</td>
              <td>{{ ev.device_id }}</td>
              <td>{{ ev.raw_seq }}</td>
              <td class="mono">{{ ev.chip }}</td>
              <td>{{ ev.node_code }}</td>
              <td>{{ treatment(ev.treatment) }}</td>
              <td class="mono" style="font-size:10px">{{ ev.issue_key ?? '' }}</td>
            </tr>
          }
        </tbody>
      </table>
    </div>
  `,
})
export class AthleteTimelineComponent {
  @Input() result!: ResultRow;

  fmtDuration = fmtDuration;
  fmtClock = fmtClock;

  statusLabel(s: string): string {
    return STATUS_LABELS[s] ?? s;
  }
  lapStatus(s: string): string {
    return { confirmed: '确认', in_review: '待核实', void: '不计圈',
             in_progress: '进行中' }[s] ?? s;
  }
  treatment(t: string): string {
    return TREATMENT_LABELS[t] ?? t;
  }

  get netBasis(): string {
    return this.result.net_start_basis === 'first_start_mat_read'
      ? '实际过毯' : '枪声兜底（起点毯缺失）';
  }

  private readonly span = computed(() => {
    const times: number[] = [];
    const push = (iso: string) => times.push(new Date(iso).getTime());
    push(this.result.gun_time);
    for (const lap of this.result.laps) {
      if (lap.start_time) push(lap.start_time);
      if (lap.finish_time) push(lap.finish_time);
    }
    for (const ev of this.result.timeline) push(ev.time);
    const min = Math.min(...times);
    const max = Math.max(...times);
    return { min, max: Math.max(max, min + 1) };
  });

  trackWidth = () =>
    Math.max(760, (this.span().max - this.span().min) / 1000 * 6 + 120);

  private x(iso: string): number {
    const { min, max } = this.span();
    const t = new Date(iso).getTime();
    return 40 + ((t - min) / (max - min)) * (this.trackWidth() - 80);
  }

  ticks = computed(() => {
    const { min, max } = this.span();
    const stepMs = chooseTickStep(max - min);
    const start = Math.ceil(min / stepMs) * stepMs;
    const out: { x: number; label: string }[] = [];
    for (let t = start; t <= max; t += stepMs) {
      out.push({
        x: this.x(new Date(t).toISOString()),
        label: new Date(t).toLocaleTimeString('zh-CN', { hour12: false }),
      });
    }
    return out;
  });

  lanes = computed(() => {
    const out: { label: string; dots: Dot[] }[] = [];
    for (const lap of this.result.laps) {
      const dots: Dot[] = [];
      const seen = new Set<number>();
      for (const ev of this.result.timeline) {
        if (!lap.segments.some((s) => s.time === ev.time && s.node_code === ev.node_code)) {
          continue;
        }
        if (seen.has(ev.read_id)) continue;
        seen.add(ev.read_id);
        dots.push({
          x: this.x(ev.time), cls: ev.treatment, code: ev.node_code,
          time: ev.time, chip: ev.chip, treatment: ev.treatment,
          issueKey: ev.issue_key,
        });
      }
      out.push({ label: `第 ${lap.lap_no} 圈 · ${this.lapStatus(lap.status)}`, dots });
    }
    if (this.result.partial_lap) {
      const dots: Dot[] = [];
      for (const ev of this.result.timeline) {
        if (!this.result.partial_lap.segments.some(
          (s) => s.time === ev.time && s.node_code === ev.node_code)) continue;
        dots.push({
          x: this.x(ev.time), cls: ev.treatment, code: ev.node_code,
          time: ev.time, chip: ev.chip, treatment: ev.treatment,
          issueKey: ev.issue_key,
        });
      }
      out.push({ label: `第 ${this.result.partial_lap.lap_no} 圈 · 进行中`, dots });
    }
    return out;
  });
}

function chooseTickStep(spanMs: number): number {
  const target = spanMs / 6;
  const candidates = [60_000, 120_000, 300_000, 600_000, 900_000,
                      1_800_000, 3_600_000];
  for (const c of candidates) if (c >= target) return c;
  return 3_600_000;
}
