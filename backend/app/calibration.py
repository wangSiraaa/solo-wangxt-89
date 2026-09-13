"""Piecewise device-clock calibration (pure functions).

A device may stamp records with a drifting clock.  A trusted *sync
observation* states "when the device clock showed D, the true time was T".
Between two observations the mapping is affine (offset + rate); outside the
observed range we still map, but only to a widening *interval* — we never
fabricate millisecond certainty where calibration cannot guarantee it.

Restarts (clock jumps backwards) are handled with segments on the *server
receive time* axis: a read received between the arrival instants of sync i
and sync i+1 belongs to that segment.  If a segment spans a reboot (the
device-time/true-time ratio is nonsensical), mapping falls back to a single
offset anchored at the segment's first sync with a widened interval.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from .timeutil import utc

# Default plausibility bound for extrapolation: a quartz/device clock is not
# expected to drift faster than this (1000 ppm ≈ 3.6 s/h).
DEFAULT_DRIFT_BOUND = 0.001


@dataclass(frozen=True)
class SyncT:
    id: int
    device_id: str
    device_time: datetime
    true_time: datetime
    epsilon_s: float
    received_at: datetime | None
    source: str = "manual"


@dataclass(frozen=True)
class TimeEstimate:
    lo: datetime
    point: datetime
    hi: datetime
    basis: str        # uncalibrated | interpolated | receive_window |
                      # segment_offset | extrapolated
    device_id: str
    segment_index: int | None
    rate: float
    offset_s: float
    uncertainty_s: float
    raw_time: datetime
    received_at: datetime | None
    sync_ids: tuple[int, ...]


def _epoch(dt: datetime) -> float:
    return utc(dt).timestamp()


def device_signatures(syncs) -> dict[str, str]:
    """Stable per-device fingerprint of calibration evidence."""
    by_dev: dict[str, list] = {}
    for s in syncs:
        by_dev.setdefault(s.device_id, []).append({
            "id": s.id,
            "device_time": utc(s.device_time).isoformat(),
            "true_time": utc(s.true_time).isoformat(),
            "epsilon_s": s.epsilon_s,
            "received_at": utc(s.received_at).isoformat()
            if s.received_at else None,
            "source": s.source,
        })
    out: dict[str, str] = {}
    for dev, rows in sorted(by_dev.items()):
        rows.sort(key=lambda r: r["id"])
        blob = json.dumps(rows, sort_keys=True, ensure_ascii=False)
        out[dev] = hashlib.sha256(blob.encode()).hexdigest()[:16]
    return out


@dataclass(frozen=True)
class _Pair:
    i: int
    a: SyncT
    b: SyncT | None
    # arrival window on the receive-time axis
    recv_lo: float | None
    recv_hi: float | None

    @property
    def rate(self):
        if self.b is None:
            return 1.0
        dd = _epoch(self.b.device_time) - _epoch(self.a.device_time)
        dt = _epoch(self.b.true_time) - _epoch(self.a.true_time)
        if abs(dd) < 1e-6:
            return None  # reboot / degenerate
        r = dt / dd
        # Rate far outside physical drift → segment crosses a clock reset.
        if r <= 0.5 or r >= 2.0:
            return None
        return r

    def map_point(self, d: float) -> tuple[float, bool]:
        """Return (point, affine?) — affine=False means offset fallback."""
        if self.b is not None and self.rate is not None:
            return _epoch(self.a.true_time) + self.rate * (
                d - _epoch(self.a.device_time)), True
        return _epoch(self.a.true_time) + (
            d - _epoch(self.a.device_time)), False


def build_device_maps(syncs, reads, *, drift_bound: float = DEFAULT_DRIFT_BOUND):
    """Return ({read_id: TimeEstimate}, {device_id: signature})."""
    signatures = device_signatures(syncs)

    by_dev: dict[str, list[SyncT]] = {}
    for s in syncs:
        by_dev.setdefault(s.device_id, []).append(s)

    pairs_by_dev: dict[str, list[_Pair]] = {}
    for dev, rows in by_dev.items():
        rows = sorted(rows, key=lambda x: (
            _epoch(x.received_at) if x.received_at else _epoch(x.true_time),
            x.id))
        pairs: list[_Pair] = []
        for i, a in enumerate(rows):
            b = rows[i + 1] if i + 1 < len(rows) else None
            pairs.append(_Pair(
                i=i, a=a, b=b,
                recv_lo=_epoch(a.received_at) if a.received_at else None,
                recv_hi=_epoch(b.received_at) if b and b.received_at else None,
            ))
        pairs_by_dev[dev] = pairs

    estimates: dict[int, TimeEstimate] = {}

    for r in reads:
        raw = utc(r.read_time)
        pairs = pairs_by_dev.get(r.device_id)
        if not pairs:
            estimates[r.id] = TimeEstimate(
                lo=raw, point=raw, hi=raw, basis="uncalibrated",
                device_id=r.device_id, segment_index=None, rate=1.0,
                offset_s=0.0, uncertainty_s=0.0, raw_time=raw,
                received_at=utc(r.received_at) if r.received_at else None,
                sync_ids=(),
            )
            continue

        d = _epoch(raw)
        recv = _epoch(r.received_at) if r.received_at else None
        rows_d = sorted(by_dev[r.device_id],
                        key=lambda x: (_epoch(x.device_time), x.id))
        est: TimeEstimate | None = None

        def mk(point, half, basis, pair, affine, sync_ids):
            hi_t = datetime.fromtimestamp(point + half, tz=raw.tzinfo)
            lo_t = datetime.fromtimestamp(point - half, tz=raw.tzinfo)
            return TimeEstimate(
                lo=lo_t,
                point=datetime.fromtimestamp(point, tz=raw.tzinfo),
                hi=hi_t, basis=basis, device_id=r.device_id,
                segment_index=pair.i if pair else None,
                rate=pair.rate if (pair and affine) else 1.0,
                offset_s=point - d, uncertainty_s=max(0.0, half),
                raw_time=raw,
                received_at=utc(r.received_at) if r.received_at else None,
                sync_ids=tuple(sync_ids),
            )

        # 1) arrival-window segment (restart-safe) when receive times exist
        if recv is not None:
            for pair in pairs:
                if pair.recv_lo is not None and \
                        pair.recv_lo <= recv and \
                        (pair.recv_hi is None or recv < pair.recv_hi):
                    point, affine = pair.map_point(d)
                    if pair.b is None:
                        # After last known sync: extrapolation beyond it.
                        dist = abs(d - _epoch(pair.a.device_time))
                        half = pair.a.epsilon_s + drift_bound * dist
                        est = mk(point, half, "extrapolated", pair, False,
                                 (pair.a.id,))
                    elif affine:
                        # Interpolation uncertainty: reference eps at anchors,
                        # small residual in between (linear, max at midpoint).
                        span = abs(_epoch(pair.b.device_time)
                                   - _epoch(pair.a.device_time))
                        frac = min(1.0, abs(d - _epoch(pair.a.device_time))
                                   / max(1e-9, span))
                        half = max(pair.a.epsilon_s,
                                   pair.b.epsilon_s) * min(1.0, 2 * frac * (1 - frac) + 1.0)
                        est = mk(point, half, "receive_window", pair, True,
                                 (pair.a.id, pair.b.id))
                    else:
                        # Segment spans a reboot: single offset, widen with
                        # distance from the segment anchor.
                        dist = abs(d - _epoch(pair.a.device_time))
                        half = pair.a.epsilon_s + drift_bound * dist
                        est = mk(point, half, "segment_offset", pair, False,
                                 (pair.a.id,))
                    break

        # 2) device-time bracketing interpolation
        if est is None:
            for j in range(len(rows_d) - 1):
                a, b = rows_d[j], rows_d[j + 1]
                dd = _epoch(b.device_time) - _epoch(a.device_time)
                dt = _epoch(b.true_time) - _epoch(a.true_time)
                if abs(dd) < 1e-6:
                    continue
                rate = dt / dd
                if rate <= 0.5 or rate >= 2.0:
                    continue
                if _epoch(a.device_time) <= d <= _epoch(b.device_time):
                    point = _epoch(a.true_time) + rate * (
                        d - _epoch(a.device_time))
                    span = abs(dd)
                    frac = min(1.0, abs(d - _epoch(a.device_time))
                               / max(1e-9, span))
                    half = max(a.epsilon_s, b.epsilon_s) * min(
                        1.0, 2 * frac * (1 - frac) + 1.0)
                    pair = next((p for p in pairs if p.a is a and p.b is b),
                                None)
                    est = mk(point, half, "interpolated", pair, True,
                             (a.id, b.id))
                    break

        # 3) extrapolation beyond device-time range
        if est is None:
            first, last = rows_d[0], rows_d[-1]
            if d <= _epoch(first.device_time):
                anchor = first
            else:
                anchor = last
            # rate from a sane pair adjacent to the anchor if available
            rate = 1.0
            near = [p for p in pairs if p.a is anchor and p.rate is not None]
            if near:
                rate = near[0].rate
            point = _epoch(anchor.true_time) + rate * (
                d - _epoch(anchor.device_time))
            dist = abs(d - _epoch(anchor.device_time))
            half = anchor.epsilon_s + drift_bound * dist
            pair = next((p for p in pairs if p.a is anchor), None)
            est = mk(point, half, "extrapolated", pair, rate != 1.0,
                     (anchor.id,))

        # Observation cannot be true after the server received it.
        if r.received_at is not None and est.hi > utc(r.received_at):
            capped = utc(r.received_at)
            point = min(est.point, capped)
            est = TimeEstimate(
                lo=est.lo, point=point, hi=capped, basis=est.basis,
                device_id=est.device_id, segment_index=est.segment_index,
                rate=est.rate, offset_s=est.offset_s,
                uncertainty_s=max(0.0, (capped - est.lo).total_seconds() / 2),
                raw_time=raw, received_at=est.received_at,
                sync_ids=est.sync_ids,
            )

        estimates[r.id] = est

    return estimates, signatures
