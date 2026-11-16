"""PROTOTYPE ONLY. Early warning: fit line + sidereal harmonic to the 48h of
fixes before the check time, predict forward, and flag when the latest fixes
depart from the prediction. Measures latency after a burn and false alarms."""
from __future__ import annotations
import math, random, statistics, sys
from datetime import timedelta, datetime, timezone
sys.path.insert(0, sys.argv[1])
from detect import synth, SID

def solve(A, b):
    n = len(b); M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(M[r][c]))
        M[c], M[p] = M[p], M[c]
        if abs(M[c][c]) < 1e-12: return None
        for r in range(n):
            if r != c:
                f = M[r][c] / M[c][c]
                M[r] = [x - f*y for x, y in zip(M[r], M[c])]
    return [M[i][n] / M[i][i] for i in range(n)]

def basis(x): return [1.0, x, math.sin(2*math.pi*x/SID), math.cos(2*math.pi*x/SID)]

def fit(train, t0):
    X = [basis((w - t0).total_seconds()/86400) for w, _ in train]; Y = [v for _, v in train]
    A = [[sum(r[i]*r[j] for r in X) for j in range(4)] for i in range(4)]
    b = [sum(r[i]*y for r, y in zip(X, Y)) for i in range(4)]
    c = solve(A, b)
    if c is None: return None
    res = [y - sum(ci*bi for ci, bi in zip(c, r)) for r, y in zip(X, Y)]
    return c, math.sqrt(sum(e*e for e in res)/max(1, len(res)-4))

def first_alarm(samples, start, stop):
    """Walk 'now' forward hourly; alarm when the median of the last 3 fixes
    departs from the forward prediction by > max(0.03, 6 * training rms)."""
    now = start
    while now <= stop:
        train = [s for s in samples if now - timedelta(hours=72) <= s[0] < now - timedelta(hours=6)]
        fresh = [s for s in samples if now - timedelta(hours=6) <= s[0] <= now][-3:]
        if len(train) >= 20 and len(fresh) == 3:
            t0 = train[0][0]; f = fit(train, t0)
            if f:
                c, rms = f
                dev = statistics.median(abs(v - sum(ci*bi for ci, bi in zip(c, basis((w-t0).total_seconds()/86400)))) for w, v in fresh)
                if dev > max(0.03, 6*rms):
                    return now
        now += timedelta(hours=1)
    return None

if __name__ == '__main__':
    rng = random.Random(7)
    end = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)
    fa = 0; quiet = 300
    for _ in range(quiet):
        l, _ = synth(rng, end, days=6)
        fa += first_alarm(l, end - timedelta(hours=48), end) is not None
    print(f"QUIET: {quiet} objects x 48 hourly checks each: false alarms on {fa} objects")
    for dr in (0.05, 0.1, 0.3, 1.0):
        lat = []; miss = 0
        for _ in range(40):
            burn = end - timedelta(hours=40)
            l, _ = synth(rng, end, days=6, dr=rng.choice([-1, 1])*dr, burn=burn)
            a = first_alarm(l, burn - timedelta(hours=6), end)
            if a is None or a < burn: miss += a is None
            if a is not None and a >= burn: lat.append((a - burn).total_seconds()/3600)
        print(f"burn {dr:>4} deg/day: alarmed {len(lat)}/40, median latency {statistics.median(lat) if lat else float('nan'):.0f} h")
