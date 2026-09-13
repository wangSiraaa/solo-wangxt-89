"""Seed a demo event and push simulated device reads.

Run while the API is up:

    python scripts/seed_demo.py            # full reset + seed, returns event id
    python scripts/seed_demo.py --http URL # against a remote API

The demo deliberately contains every tricky case:
* mixed category with sub-classes, two staggered waves (net vs gun);
* bib 301 standing on the finish mat (tight duplicate cluster);
* bib 102 a late finish re-hit (real extra lap? held as REPEAT_FINISH);
* bib 201 a mid-race chip swap to CCC-X plus a reverse stray record;
* bib 302 an unknown chip (typo) and a missed intermediate node.
Nothing is auto-adjudicated; the operator works the issues in the UI.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone

import httpx

T0 = datetime(2026, 9, 13, 8, 0, 0, tzinfo=timezone.utc)
M = 60.0


def ts(s: float) -> str:
    return (T0 + timedelta(seconds=s)).isoformat()


class Sim:
    def __init__(self, base: str):
        self.http = httpx.Client(base_url=base, timeout=10)
        self.seq = 0

    def read(self, chip, node, sec, device="MAT"):
        self.seq += 1
        return {
            "chip": chip, "node_code": node, "device_id": device,
            "raw_seq": self.seq, "read_time": ts(sec),
            "idempotency_key": f"SIM-{self.seq:04d}-{chip}-{node}",
        }


def create_event(http) -> dict:
    payload = {
        "name": "环湖耐力挑战赛（演示）2026-09-13",
        "waves": [
            {"name": "第一批 08:00", "gun_time": ts(0)},
            {"name": "第二批 08:15", "gun_time": ts(15 * M)},
        ],
        "categories": [
            {"name": "混合公开组", "laps_required": 2, "mixed": True},
            {"name": "女子组", "laps_required": 2, "rank_by": "gun"},
            {"name": "体验组", "laps_required": 1},
        ],
        "courses": [
            {
                "name": "环湖主环 S-CP1-CP2-F",
                "nodes": [
                    {"order_index": 0, "code": "S", "name": "起点/换圈",
                     "kind": "start", "min_split_s": 0},
                    {"order_index": 1, "code": "CP1", "name": "1号打卡点",
                     "kind": "control", "min_split_s": 6 * M},
                    {"order_index": 2, "code": "CP2", "name": "2号打卡点",
                     "kind": "control", "min_split_s": 5 * M},
                    {"order_index": 3, "code": "F", "name": "终点",
                     "kind": "finish", "min_split_s": 4 * M,
                     "max_split_s": 4 * 3600},
                ],
            }
        ],
        "competitors": [],
    }
    r = http.post("/api/events", json=payload)
    r.raise_for_status()
    eid = r.json()["event_id"]

    detail = http.get(f"/api/events/{eid}").json()
    w1 = detail["waves"][0]["id"]
    w2 = detail["waves"][1]["id"]
    c_mix = next(c["id"] for c in detail["categories"] if c["name"] == "混合公开组")
    c_wom = next(c["id"] for c in detail["categories"] if c["name"] == "女子组")
    c_fun = next(c["id"] for c in detail["categories"] if c["name"] == "体验组")
    course = detail["courses"][0]["id"]

    regs = [
        {"bib": "A001", "name": "张磊", "category_id": c_mix, "wave_id": w1,
         "course_id": course, "initial_chip": "CHIP-A001",
         "class_label": "男子精英"},
        {"bib": "A002", "name": "李娜", "category_id": c_mix, "wave_id": w2,
         "course_id": course, "initial_chip": "CHIP-A002",
         "class_label": "女子"},
        {"bib": "B001", "name": "王芳", "category_id": c_wom, "wave_id": w2,
         "course_id": course, "initial_chip": "CHIP-B001"},
        {"bib": "C001", "name": "赵强", "category_id": c_fun, "wave_id": w1,
         "course_id": course, "initial_chip": "CHIP-C001"},
        {"bib": "C002", "name": "孙敏", "category_id": c_fun, "wave_id": w1,
         "course_id": course, "initial_chip": "CHIP-C002"},
    ]
    r = http.post(f"/api/events/{eid}/competitors", json=regs)
    r.raise_for_status()
    return http.get(f"/api/events/{eid}").json()


def build_reads() -> list[dict]:
    sim = Sim("")
    R = sim.read
    reads: list[dict] = []

    # ---- A001 wave 1 (gun 0): two clean laps, crosses start at the gun --
    # lap1 38:20, lap2 37:50
    a1 = [("S", 0), ("CP1", 600), ("CP2", 1320), ("F", 2300)]
    lap2 = [("S", 2360), ("CP1", 3000), ("CP2", 3720), ("F", 4630)]
    for n, s in a1 + lap2:
        reads.append(R("CHIP-A001", n, s))

    # ---- A002 wave 2 (gun 900), start mat crossed 45 s late ----------
    b1 = [("S", 945), ("CP1", 1900), ("CP2", 2900), ("F", 3900)]
    b2 = [("S", 3960), ("CP1", 4900), ("CP2", 5900), ("F", 6900)]
    for n, s in b1 + b2:
        reads.append(R("CHIP-A002", n, s))
    # ... 41 minutes after finishing, finish mat fires again with no
    # intermediate reads: did she run another lap or stand on the mat?
    reads.append(R("CHIP-A002", "F", 9360))

    # ---- B001 wave 2: lap 1 clean, chip replaced mid-race ------------
    c1 = [("S", 900), ("CP1", 1800), ("CP2", 2800), ("F", 3700)]
    for n, s in c1:
        reads.append(R("CHIP-B001", n, s))
    # New chip from the second lap start; CP2 device sends one stray
    # out-of-order record afterwards (reverse order issue).
    c2 = [("S", 3760), ("CP1", 4700), ("CP2", 5700), ("F", 6700)]
    for n, s in c2:
        reads.append(R("CHIP-B001-X", n, s))
    reads.append(R("CHIP-B001-X", "CP1", 5740))  # stray backwards record

    # ---- C001 wave 1: one lap, stands on the finish mat (4s gap) ------
    for n, s in [("S", 0), ("CP1", 900), ("CP2", 1900), ("F", 2700)]:
        reads.append(R("CHIP-C001", n, s))
    reads.append(R("CHIP-C001", "F", 2704))  # duplicate cluster

    # ---- C002 wave 1: missed CP1 (forward jump) + chip typo read ------
    # CHIP-C020 (should be C002) appears at the start → unknown chip;
    # CP1 absent while CP2 then F exist → missing node.
    reads.append(R("CHIP-C020", "S", 0))
    reads.append(R("CHIP-C002", "CP2", 1800))
    reads.append(R("CHIP-C002", "F", 2700))

    return reads


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--http", default="http://127.0.0.1:8000")
    args = ap.parse_args()

    with httpx.Client(base_url=args.http, timeout=10) as http:
        event = create_event(http)
        eid = event["id"]
        reads = build_reads()
        r = http.post(f"/api/events/{eid}/device-reads",
                      json={"event_id": eid, "reads": reads})
        r.raise_for_status()
        print(f"event_id={eid} inserted={r.json()['inserted']} "
              f"duplicates={r.json()['duplicates_ignored']}")
        replay = http.post(f"/api/events/{eid}/replay").json()
        print(f"input_hash={replay['input_hash'][:16]}… "
              f"output_hash={replay['output_hash'][:16]}…")
        issues = replay["output"]["issues"]
        print(f"open issues: {sum(i['resolution'] is None for i in issues)}")
        for i in issues:
            if i["resolution"] is None:
                comp = next((r["bib"] for r in replay["output"]["results"]
                             if r["competitor_id"] == i["competitor_id"]), "?")
                print(f"  [{comp}] {i['issue_key']}  {i['title']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
