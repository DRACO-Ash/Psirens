"""PROTOTYPE ONLY (scratchpad, not shipped). Can an event detector built on
psirens.astro.drift_fit find GEO manoeuvres in noisy, libration-heavy data
without crying wolf on objects that are simply sitting on station?"""
from __future__ import annotations
import math, random, statistics, sys
from datetime import datetime, timedelta, timezone
sys.path.insert(0, "/home/user/Psirens/src")
from psirens.astro import drift_fit

SID = 0.9972695787
EW_FLOOR = 0.02      # deg/day: below this a rate change is station-keeping noise
NS_FLOOR = 0.008     # deg: smallest inclination step worth an operator's time
SIGMA_K = 6.0        # significance multiple required on top of the floor
WIN_H = 48           # hours each side of a candidate split

def rate_sigma(fit):
    # standard error of a slope: rms * sqrt(12) / (span * sqrt(n))
    span_d = max(fit.span_hours / 24.0, 1e-6)
    return fit.residual_deg * math.sqrt(12.0) / (span_d * math.sqrt(max(fit.points, 3)))

def ew_event(samples):
    """Best E-W split: biggest significant change in fitted drift rate."""
    if len(samples) < 20:
        return None
    t0, t1 = samples[0][0], samples[-1][0]
    best = None
    t = t0 + timedelta(hours=WIN_H)
    while t <= t1 - timedelta(hours=24):
        before = [s for s in samples if t - timedelta(hours=WIN_H) <= s[0] < t]
        after = [s for s in samples if t <= s[0] < t + timedelta(hours=WIN_H)]
        fb, fa = drift_fit(before, window_days=WIN_H/24), drift_fit(after, window_days=WIN_H/24)
        if fb and fa:
            d = fa.rate_deg_per_day - fb.rate_deg_per_day
            sig = math.hypot(rate_sigma(fb), rate_sigma(fa)) or 1e-9
            z = abs(d) / sig
            if abs(d) > EW_FLOOR and z > SIGMA_K and (best is None or z > best[3]):
                best = (t, fb.rate_deg_per_day, fa.rate_deg_per_day, z)
        last = t
        t += timedelta(hours=3)
    if best is None:
        return None
    # EDGE RULE. If the evidence peaks at the LAST split we could test, the
    # after-window may straddle a burn that happened later: the time and the
    # new rate are then both wrong, confidently. Caught by the prototype on an
    # injected 1.0 deg/day burn 7 h before the end, which was "confirmed" 17 h
    # early at 0.31 deg/day. Demote, never confirm.
    edge = (last - best[0]) <= timedelta(hours=3)
    return best + (edge,)

def ns_event(samples_inc):
    if len(samples_inc) < 20:
        return None
    t0, t1 = samples_inc[0][0], samples_inc[-1][0]
    best = None
    t = t0 + timedelta(hours=24)
    while t <= t1 - timedelta(hours=12):
        b = [v for w, v in samples_inc if t - timedelta(hours=WIN_H) <= w < t]
        a = [v for w, v in samples_inc if t <= w < t + timedelta(hours=WIN_H)]
        if len(b) >= 8 and len(a) >= 6:
            d = statistics.median(a) - statistics.median(b)
            mad = statistics.median([abs(v - statistics.median(b)) for v in b]) * 1.4826 or 1e-4
            z = abs(d) / (mad * math.sqrt(1/len(a) + 1/len(b)) * 1.25)
            if abs(d) > NS_FLOOR and z > SIGMA_K and (best is None or z > best[2]):
                best = (t, d, z)
        t += timedelta(hours=3)
    return best

def synth(rng, end, days=10, dr=0.0, burn=None, di=0.0):
    """One GEO object: libration at the sidereal period, two-provider bias,
    per-fix scatter, ragged hourly sampling with dropouts, optional burn."""
    e = rng.uniform(0.0002, 0.003); ph = rng.uniform(0, 2*math.pi)
    amp = math.degrees(2*e)
    rate0 = rng.gauss(0, 0.004)
    bias = {0: 0.0, 1: rng.uniform(-0.012, 0.012)}
    sp = rng.uniform(0.004, 0.02); si = rng.uniform(0.001, 0.004)
    inc0 = rng.uniform(0.02, 4.0)
    lon0 = rng.uniform(-180, 180)
    out_l, out_i = [], []
    t = end - timedelta(days=days)
    while t <= end:
        if rng.random() > 0.3:
            d = (t - (end - timedelta(days=days))).total_seconds()/86400
            lon = lon0 + rate0*d + amp*math.sin(2*math.pi*d/SID + ph)
            inc = inc0
            if burn is not None and t >= burn:
                db = (t - burn).total_seconds()/86400
                lon += dr*db; inc += di
            prov = rng.randint(0, 1)
            out_l.append((t, lon + bias[prov] + rng.gauss(0, sp)))
            out_i.append((t, inc + rng.gauss(0, si)))
        t += timedelta(minutes=rng.choice([45, 60, 60, 75, 90]))
    return out_l, out_i

def main():
    rng = random.Random(20261116)
    end = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)
    quiet = 400
    fp_ew = fp_ns = 0
    for _ in range(quiet):
        l, i = synth(rng, end)
        fp_ew += ew_event(l) is not None
        fp_ns += ns_event(i) is not None
    print(f"QUIET objects: {quiet}  false E-W events: {fp_ew}  false N-S events: {fp_ns}")
    for dr in (0.02, 0.05, 0.1, 0.3):
        hit = 0; errs = []
        n = 60
        for _ in range(n):
            burn = end - timedelta(hours=rng.uniform(36, 200))
            l, i = synth(rng, end, dr=rng.choice([-1, 1])*dr, burn=burn)
            ev = ew_event(l)
            if ev:
                hit += 1; errs.append(abs((ev[0]-burn).total_seconds())/3600)
        med = statistics.median(errs) if errs else float("nan")
        print(f"E-W burn |d rate| {dr:>5} deg/day: detected {hit}/{n}  median timing error {med:.1f} h")
    for di in (0.005, 0.01, 0.03, 0.1):
        hit = 0; n = 60
        for _ in range(n):
            burn = end - timedelta(hours=rng.uniform(36, 200))
            l, i = synth(rng, end, di=rng.choice([-1, 1])*di, burn=burn)
            hit += ns_event(i) is not None
        print(f"N-S burn |d inc|  {di:>5} deg    : detected {hit}/{n}")

if __name__ == "__main__":
    main()
