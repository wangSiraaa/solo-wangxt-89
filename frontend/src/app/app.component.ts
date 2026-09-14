import { Component } from '@angular/core';
import { Router, RouterLink, RouterLinkActive, RouterOutlet } from '@angular/router';
import { EventState } from './event.state';

@Component({
  selector: 'app-root',
  standalone: true,
  imports: [RouterOutlet, RouterLink, RouterLinkActive],
  template: `
    <header class="app-header">
      <h1>耐力赛计时复核台</h1>
      @if (state.eventId(); as eid) {
        <nav class="tabs" style="margin-bottom:0">
          <a [routerLink]="['/event', eid, 'review']"
             routerLinkActive="active">疑点逐项确认</a>
          <a [routerLink]="['/event', eid, 'timeline']"
             routerLinkActive="active">选手时间线</a>
          <a [routerLink]="['/event', eid, 'leaderboard']"
             routerLinkActive="active">榜单 / 发布</a>
          <a [routerLink]="['/event', eid, 'clock']"
             routerLinkActive="active">设备对时</a>
          <a [routerLink]="['/event', eid, 'history']"
             routerLinkActive="active">人工裁定记录</a>
        </nav>
        <span class="hash" style="margin-left:auto">
          时钟 {{ state.clockHash() }} · 身份 {{ state.identityHash() }} ·
          裁定 {{ state.decisionsHash() }} · 待核实 {{ state.openIssues().length }}
          @if (state.recalibrationPending().length) {
            · <span style="color:var(--warn)">重核验 {{ state.recalibrationPending().length }}</span>
          }
        </span>
      } @else {
        <a routerLink="/" class="muted">← 选择赛事</a>
      }
    </header>
    <main class="container">
      @if (state.error()) {
        <div class="toast-error">
          {{ state.error() }}
          <button class="secondary" style="margin-left:10px"
                  (click)="state.clearError()">知道了</button>
        </div>
      }
      @if (state.loading()) {
        <div class="muted">重放计算中…</div>
      }
      <router-outlet />
    </main>
  `,
})
export class AppComponent {
  constructor(public state: EventState, router: Router) {
    // One-shot deep-link restore (a reload on /event/:id/...). Done in the
    // constructor — outside any reactive context — so opening state (which
    // writes signals) does not violate effect purity (NG0600).
    if (this.state.eventId() === null) {
      const m = router.url.match(/^\/event\/(\d+)\//);
      if (m) {
        this.state.syncTo(Number(m[1]));
      }
    }
  }
}
