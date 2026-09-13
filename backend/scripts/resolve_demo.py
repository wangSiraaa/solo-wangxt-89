"""Resolve every open issue of the latest demo event, then publish.

Demonstrates the full operator workflow:
  repeat finish  -> credit a real extra lap (with recorded reason)
  finish cluster -> keep first read (athlete stood on the mat)
  chip swap      -> attach new chip to the stable identity
  reverse read   -> reject stray record
  unknown chip   -> reject (chip typo), missing nodes credited manually

Usage: python scripts/resolve_demo.py [event_id] [--base http://127.0.0.1:8000]
"""
from __future__ import annotations

import argparse
import sys

import httpx


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("event_id", nargs="?", type=int)
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    args = ap.parse_args()

    with httpx.Client(base_url=args.base, timeout=10) as http:
        eid = args.event_id
        if eid is None:
            events = http.get("/api/events").json()
            eid = events[-1]["id"]
        print(f"event {eid}")

        def replay():
            return http.post(f"/api/events/{eid}/replay").json()

        def decide(key, decision, reason, *, competitor_id=None,
                   valid_from=None):
            body = {
                "issue_key": key, "decision": decision, "reason": reason,
                "decided_by": "chief-judge",
            }
            if competitor_id is not None:
                body["competitor_id"] = competitor_id
            if valid_from is not None:
                body["valid_from"] = valid_from
            r = http.post(
                f"/api/events/{eid}/issues/{key}/adjudications", json=body)
            print(f"  {key} -> {decision}: {r.status_code}")
            r.raise_for_status()

        def open_issues():
            return [i for i in replay()["output"]["issues"]
                    if i["resolution"] is None]

        def bib_to_id():
            d = http.get(f"/api/events/{eid}").json()
            return {c["bib"]: c["id"] for c in d["competitors"]}

        ids = bib_to_id()

        # 1. chip swap B001 -> CHIP-B001-X (before the reverse read appears)
        for issue in open_issues():
            if issue["detail"].get("chip") == "CHIP-B001-X":
                decide(issue["issue_key"], "ATTACH_CHIP",
                       "检录处换发备用芯片，选手身份 B001 不变，第二圈起生效",
                       competitor_id=ids["B001"],
                       valid_from="2026-09-13T09:02:40+00:00")
                break

        # 2. walk the remaining open issues
        for issue in open_issues():
            kind = issue["kind"]
            key = issue["issue_key"]
            if kind == "REPEAT_FINISH":
                decide(key, "CREDIT_LAP",
                       "终点录像与沿途裁判确认 A002 确实再完成一圈")
            elif kind == "DUPLICATE_READ":
                decide(key, "KEEP_FIRST",
                       "终点录像确认选手原地停留 4 秒，未再进入赛道")
            elif kind == "REVERSE_ORDER":
                decide(key, "REJECT_READ",
                       "顺序倒退，判为 CP1 设备重发的杂散记录")
            elif kind == "UNKNOWN_CHIP" and issue["detail"].get("chip") == "CHIP-C020":
                decide(key, "REJECT_CHIP",
                       "芯片号与报名 C002 不符且无换芯记录，按非本场芯片剔除")
            elif kind == "MISSING_NODE":
                decide(key, "CREDIT_NODE",
                       f"点位裁判确认选手经过 {issue['detail']['node_code']}，设备漏读")
            elif kind == "IMPLAUSIBLE_SPLIT":
                decide(key, "CONFIRM_READ", "分段核对，用时属实")
            else:
                print(f"  ! unhandled {kind} {key}")

        left = open_issues()
        if left:
            print("still open:", [(i["kind"], i["issue_key"]) for i in left])
            return 1

        r = http.post(f"/api/events/{eid}/publish",
                      json={"decided_by": "chief-judge"})
        print("publish:", r.status_code, r.json() if r.status_code == 200
              else r.text)
        r.raise_for_status()

        pub = http.get(f"/api/events/{eid}/results/published").json()
        print(f"published v{pub['version']}")
        for e in pub["entries"]:
            print(f"  {e['bib']:>5} {e['name']:<6} {e['status']:<9} "
                  f"laps={e['laps_confirmed']} net={e['total_net_s']} "
                  f"gun={e['total_gun_s']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
