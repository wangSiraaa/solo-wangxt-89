# 耐力赛事计时复核系统

终点感应器会对同一芯片连续读卡：可能是选手站在感应区，也可能确实又跑完一圈。
本系统不做"自动算数/自动取消"，而是把**原始读卡**与**人工裁定**完整留存，
由一个**确定性的圈次重建引擎**重放出候选圈次、疑点与榜单；管理员逐项确认后
才能发布成绩。任何最终成绩都不能靠直接改数字产生。

## 设计原则（对应需求）

| 需求 | 实现 |
| --- | --- |
| 原始读卡不可篡改 | `chip_reads` 只追加；API 没有更新/删除读卡的接口，批量上报按 `idempotency_key` 幂等 |
| 终点连续读卡二义性 | 8 秒窗口内同点多次读卡折叠为一个"读卡簇"，出 `DUPLICATE_READ` 疑点，由人选择采用最早/最晚一条；间隔很大且整圈无内部读卡的终点再触发出 `REPEAT_FINISH`（新的一圈 vs 停在终点），默认挂起 |
| 结合路线节点与可行用时 | 每圈节点按顺序推进；节点配置 `min_split_s`/`max_split_s`（过快=不可信、过慢=超关门），违反出 `IMPLAUSIBLE_SPLIT` |
| 漏点 / 逆向记录待核实 | 前向跳点出 `MISSING_NODE`（认可漏点 / 该圈不计），顺序倒退出 `REVERSE_ORDER`（接受 / 剔除）；未裁定前圈保持 `in_review`，**绝不自动取消成绩** |
| 原始读卡与人工裁定入 PostgreSQL | 全部数据在 PostgreSQL；`adjudications` 只追加，同一疑点可反复裁定，最新一条生效，旧记录始终保留 |
| FastAPI 接收模拟设备数据 | `POST /api/events/{id}/device-reads`，配套 `backend/scripts/seed_demo.py` 模拟器 |
| Angular 呈现时间线/疑点/榜单 | 独立 Angular 18 前端：逐点确认页、选手泳道时间线、榜单与发布、裁定历史 |
| 分批出发净/枪计时分开 | 每批一个 `wave` 与枪声时间；净计时从**首次起点毯实际过毯**开始（缺失时退回枪声并标注依据），枪声计时从本批枪声开始；组别可选择按哪种排名 |
| 换芯片按稳定身份衔接 | 选手是稳定身份，芯片是时间段挂接（`chip_assignments`）；新芯片先出 `UNKNOWN_CHIP`，`ATTACH_CHIP` 裁定后把读卡归属到同一选手，时间线显示芯片切换 |
| 逐项确认后发布，禁止直接改最终数字 | 有未决疑点时 `POST .../publish` 返回 409 并列出全部疑点；成绩快照由重放结果生成（带输入哈希），系统没有任何编辑最终时间的接口；裁判长取消成绩走 `official-adjudication`（DSQ 留痕） |
| 混合组别 | `mixed` 组别同时给总名次与子组（`class_label`）名次；待核实/DSQ 不占位 |
| 重放同一输入得到相同候选圈次 | `app/engine.py` 是纯函数（无 I/O、不读时钟），对规范输入与输出分别计算 SHA-256；重放结果存 `replay_runs`，界面顶部显示输入/输出哈希 |
| 判定依据界面可查 | 每个疑点卡片展示原始证据明细（读卡列表、区间、期望/实际顺序等）、裁定选项、已有裁定的人/时间/理由/载荷；另有独立的裁定历史页 |

## 目录

```
backend/
  app/
    engine.py        # 确定性圈次重建（纯函数 + 哈希）
    models.py        # SQLAlchemy 模型（PostgreSQL JSONB，测试可退化）
    repo.py          # DB → 引擎 Bundle；落盘 replay_runs
    main.py          # FastAPI（设备上报/重放/裁定/发布）
    schemas.py       # 请求模型
  scripts/
    seed_demo.py     # 建演示赛事并推送模拟读卡（含全部异常场景）
    resolve_demo.py  # 逐项裁定全部疑点并发布（完整操作演练）
  tests/test_scenarios.py
frontend/            # Angular 18 独立组件前端
run_local.sh         # 免 root 启动便携 PostgreSQL + API
```

## 本地运行

需要 Python 3.11 与 Node 20。本机演示用的 PostgreSQL 15 是以普通用户
解压 Debian deb 包方式运行的（端口 55432，库名 `timing`）。

```bash
# 1) 后端（首次会自动准备便携 PostgreSQL）
pip install --user -r backend/requirements.txt
./run_local.sh                       # API: http://127.0.0.1:8000  (docs: /docs)

# 2) 前端（另开终端）
cd frontend && npm install && npm start
# UI:  http://127.0.0.1:4200  （/api 代理到 8000）

# 3) 造一场包含全部场景的演示赛事
python3 backend/scripts/seed_demo.py
# 4)（可选）自动把所有疑点逐项裁定并发布，观察完整流程
python3 backend/scripts/resolve_demo.py
```

`DATABASE_URL` 可覆盖数据库连接
（默认 `postgresql+psycopg2://timing@/timing?host=/tmp&port=55432`）。

## 验证（测试）

```bash
cd backend && python3 -m pytest -q
```

覆盖：混合组别总名次/子组名次、净计时与枪声计时分离、终点站垫重复簇、
终点再触发计真圈（先拒后认的追加裁定历史）、中途换芯片身份衔接、
逆向记录+漏点挂起且禁止提前发布、不可信分段、**同输入重放哈希一致与批量幂等**、
成绩快照由重放生成、DSQ 只留痕不改数。

## 关键工作流

1. 建赛事：批次（枪声）、组别（圈数/混合/排名依据）、路线节点（可行分段区间）、
   选手（组别、批次、初始芯片）。
2. 设备数据持续幂等上报。
3. 打开"疑点逐项确认"：每张卡片先看判定依据（原始证据 JSON），选择处置、
   填写理由与裁定人；换芯片选择挂接选手。
4. "选手时间线"核对泳道、候选圈次、每圈节点链、漏点、芯片切换与每条原始读卡的处置。
5. 全部疑点清零后在"榜单/发布"重放并发布；发布快照带输入哈希，可回溯到当时的
   完整证据与裁定。
