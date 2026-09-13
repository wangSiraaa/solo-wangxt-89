"""Demonstrate the clock-correction workflow.

Creates a two-athlete race whose finish mats drift in opposite directions.
The uncorrected board publishes with one winner; after trusted sync events
are added, replay reverses the finishing order and the page can compare the
old published board against the revised board with its layered hashes.

Run after the API is up:  python scripts/clock_demo.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

import httpx

T0 = datetime(2026, 9, 13, 8, 0, 0, tzinfo=timezone.utc)
M = 60.0


def ts(s: float) -> str:
    return (T0 + timedelta(seconds=s)).isoformat()


class Sim:
    def __init__(self):
        self.n = 0

    def read(self, chip, node, sec, device, recv):
        self.n += 1
        return {
            "chip": chip, "node_code": node, "device_id": device,
            "raw_seq": self.n, "read_time": ts(sec),
            "received_at": ts(recv),
            "idempotency_key": f"CD-{self.n:03d}-{chip}-{node}",
        }

    def sync(self, device, dev_s, true_s, recv, eps=0.0):
        self.n += 1
        return {
            "device_id": device,
            "device_time": ts(dev_s), "true_time": ts(true_s),
            "reference_epsilon_s": eps,
            "received_at": ts(recv), "source": "ntp",
            "recorded_by": "timekeeper",
            "idempotency_key": f"CD-SYNC-{device}-{self.n:03d}",
        }


def main() -> int:
    with httpx.Client(base_url="http://127.0.0.1:8000", timeout=10) as h:
        eid = h.post("/api/events", json={
            "name": "时钟漂移修正演示",
            "waves": [{"name": "同批", "gun_time": ts(0)}],
            "categories": [{"name": "混开组", "laps_required": 1, "mixed": True}],
            "courses": [{
                "name": "环线",
                "nodes": [
                    {"order_index": 0, "code": "S", "name": "起点",
                     "kind": "start", "min_split_s": 0},
                    {"order_index": 1, "code": "CP1", "name": "点1",
                     "kind": "control", "min_split_s": 4 * M},
                    {"order_index": 2, "code": "CP2", "name": "点2",
                     "kind": "control", "min_split_s": 4 * M},
                    {"order_index": 3, "code": "F", "name": "终点",
                     "kind": "finish", "min_split_s": 3 * M,
                     "max_split_s": 4 * 3600},
                ],
            }],
            "competitors": [],
        }).json()["event_id"]
        d = h.get(f"/api/events/{eid}").json()
        w, c, co = d["waves"][0]["id"], d["categories"][0]["id"], d["courses"][0]["id"]
        h.post(f"/api/events/{eid}/competitors", json=[
            {"bib": "A", "name": "甲(设备快)", "category_id": c, "wave_id": w,
             "course_id": co, "initial_chip": "CA", "class_label": "男"},
            {"bib": "B", "name": "乙(设备慢)", "category_id": c, "wave_id": w,
             "course_id": co, "initial_chip": "CB", "class_label": "女"},
        ]).raise_for_status()

        sim = Sim()
        # raw stamps make A look first (2941 < 2973)
        reads = [
            sim.read("CA", "S", 0, "MID", 6),
            sim.read("CA", "CP1", 600, "MID", 606),
            sim.read("CA", "CP2", 1200, "MID", 1206),
            sim.read("CA", "F", 2941.176, "FA", 3006),
            sim.read("CB", "S", 10, "MID", 16),
            sim.read("CB", "CP1", 620, "MID", 626),
            sim.read("CB", "CP2", 1230, "MID", 1236),
            sim.read("CB", "F", 2972.917, "FB", 3002),
        ]
        h.post(f"/api/events/{eid}/device-reads",
               json={"event_id": eid, "reads": reads}).raise_for_status()

        p0 = h.post(f"/api/events/{eid}/replay").json()
        board0 = {e["bib"]: e["rank"]
                  for e in p0["output"]["leaderboard"][0]["entries"]}
        print("校时前名次:", board0, " 分层哈希:",
              {k: v[:8] for k, v in p0["component_hashes"].items()})
        h.post(f"/api/events/{eid}/publish",
               json={"decided_by": "chief"}).raise_for_status()
        print("已发布旧榜 v1")

        syncs = [
            sim.sync("FA", 2400, 2400, 2406),
            sim.sync("FA", 3600, 3624, 3630),   # +24 s drift → rate 1.02
            sim.sync("FB", 2400, 2400, 2406),
            sim.sync("FB", 3600, 3552, 3558),   # -48 s drift → rate 0.96
        ]
        res = h.post(f"/api/events/{eid}/clock-syncs",
                     json={"syncs": syncs}).json()
        print("对时写入:", res["inserted"],
              " 待重核验疑点:", res["context_changed_issues"])

        p1 = h.post(f"/api/events/{eid}/replay").json()
        b1 = p1["output"]["leaderboard"][0]["entries"]
        for e in sorted(b1, key=lambda x: x["rank"]):
            print(f"  校时后 #{e['rank']} {e['bib']} 净 {e['total_net_s']}s "
                  f"区间[{e['total_net_lo']},{e['total_net_hi']}]")
        cmp = h.get(f"/api/events/{eid}/results/compare").json()
        for r in cmp["rows"]:
            print(f"  对比 {r['bib']}: 旧 #{r['old_rank']} → 新 #{r['new_rank']}"
                  f" (Δ {r['rank_delta']}) {r['tie_label'] or ''}")
        print(f"event_id={eid}  前端「榜单/发布」可查看旧榜→修订榜对比")
    return 0


if __name__ == "__main__":
    sys.exit(main())
