import { Component, computed, effect } from '@angular/core';
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
          <a [routerLink]="['/event', eid, 'history']"
             routerLinkActive="active">人工裁定记录</a>
        </nav>
        <span class="hash" style="margin-left:auto">
          输入 {{ state.inputHash().slice(0, 12) }} ·
          输出 {{ state.outputHash().slice(0, 12) }}
          · 待核实 {{ state.openIssues().length }}
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
  constructor(public state: EventState, private router: Router) {
    // keep deep links / reloads working
    effect(() => {
      if (this.state.eventId() === null) {
        const m = this.router.url.match(/^\/event\/(\d+)\//);
        if (m) this.state.open(Number(m[1]));
      }
    });
  }
}
