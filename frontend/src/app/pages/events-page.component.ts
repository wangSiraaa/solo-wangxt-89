import { Component, OnInit, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import { DatePipe } from '@angular/common';
import { ApiService } from '../api.service';
import { EventSummary } from '../models';

@Component({
  standalone: true,
  imports: [RouterLink, DatePipe],
  template: `
    <div class="panel">
      <h2>赛事列表
        <button class="secondary" style="margin-left:auto"
                (click)="load()">刷新</button>
      </h2>
      <table>
        <thead><tr><th>ID</th><th>名称</th><th>创建时间</th><th></th></tr></thead>
        <tbody>
          @for (e of events(); track e.id) {
            <tr>
              <td>{{ e.id }}</td>
              <td>{{ e.name }}</td>
              <td class="muted">{{ e.created_at | date:'yyyy-MM-dd HH:mm' }}</td>
              <td><a [routerLink]="['/event', e.id, 'review']">进入复核 →</a></td>
            </tr>
          } @empty {
            <tr><td colspan="4" class="muted">
              暂无赛事。先通过 POST /api/events 或 scripts/seed_demo.py 建立。
            </td></tr>
          }
        </tbody>
      </table>
    </div>
  `,
})
export class EventsPageComponent implements OnInit {
  readonly events = signal<EventSummary[]>([]);

  constructor(private api: ApiService) {}

  ngOnInit(): void {
    this.load();
  }

  load(): void {
    this.api.listEvents().subscribe((rows) => this.events.set(rows));
  }
}
