"""From the new-logic alerts list, scan thresholds to show
'fewer alerts but more certain' trade-off."""
from __future__ import annotations

# Output of the prior replay (score, g4h, g24h) for the 47 strict alerts.
ALERTS = [
    ("USDUC/EUR", 76.1, 46.58, 46.58),
    ("STRK/EUR",  70.9, 10.73, 10.73),
    ("XRT/EUR",   72.6, 44.92, 44.92),
    ("ASRR/EUR",  73.9, 10.05, 10.05),
    ("TRX/EUR",   73.6,  0.20,  0.20),
    ("MOVR/EUR",  70.2,  9.23,  9.23),
    ("ONDO/EUR",  76.7,  7.48,  7.48),
    ("PROS/EUR",  70.3, 52.05, 52.05),
    ("STRK/EUR",  75.3,  8.03,  8.03),
    ("LOCKIN/EUR",74.9, 32.48, 32.48),
    ("CHIP/EUR",  75.0, 10.07, 10.07),
    ("SGB/EUR",   84.8, 17.73, 17.73),
    ("ELIZAOS/EUR",87.3,38.89, 38.89),
    ("UP/EUR",    82.1, 13.56, 13.56),
    ("NEAR/EUR",  71.8,  0.82,  0.82),
    ("FIL/EUR",   76.6, 11.10, 11.10),
    ("ADA/EUR",   75.0,  1.92,  1.92),
    ("NIGHT/EUR", 77.9,  0.64,  0.64),
    ("ONDO/EUR",  71.2,  5.35,  5.35),
    ("PLUME/EUR", 75.9,  9.32,  9.32),
    ("RNBW/EUR",  76.6, 30.76, 30.76),
    ("TAO/EUR",   74.0,  0.23,  0.23),
    ("OP/EUR",    76.3,  9.67,  9.67),
    ("RSC/EUR",   88.6, 38.13, 38.13),
    ("SUI/EUR",   78.2,  0.98,  0.98),
    ("SOL/EUR",   81.2,  1.22,  1.22),
    ("LINK/EUR",  72.0,  1.73,  1.73),
    ("XRP/EUR",   72.5,  0.94,  0.94),
    ("AVAX/EUR",  72.5,  1.31,  1.31),
    ("CCD/EUR",   71.6,  3.19,  3.19),
    ("ICP/EUR",   81.4, 12.22, 12.22),
    ("RNBW/EUR",  72.2, 55.40, 55.40),
    ("ONDO/EUR",  79.2, 13.09, 13.09),
    ("ICP/EUR",   72.1, 12.83, 12.83),
    ("ATOM/EUR",  76.4,  0.40,  0.40),
    ("CFG/EUR",   79.1, 16.78, 16.78),
    ("SUI/EUR",   86.8,  2.61,  2.61),
    ("JUP/EUR",   70.4,  4.52,  4.52),
    ("COMP/EUR",  71.8,  0.45,  0.45),
    ("LINK/EUR",  72.0,  0.25,  0.25),
    ("VVV/EUR",   83.6, 10.75, 10.75),
    ("PROS/EUR",  83.6, 19.66, 19.66),
    ("SAHARA/EUR",85.1, 14.72, 14.72),
    ("PLAY/EUR",  83.9, 27.33, 27.33),
    ("SGB/EUR",   74.8, 31.17, 31.17),
    ("SWEAT/EUR", 70.8, 91.82, 91.82),
    ("CC/EUR",    79.1,  1.28,  1.28),
]

print(f"{'threshold':>10} {'#alerts':>8} {'hit>=5/4h':>10} {'hit>=10/4h':>11} {'hit>=20/4h':>11} {'mean g4h%':>10}")
for thr in [70, 72, 74, 75, 78, 80, 82, 85]:
    sub = [a for a in ALERTS if a[1] >= thr]
    n = len(sub)
    if n == 0:
        continue
    h5 = sum(1 for a in sub if a[2] >= 5)
    h10 = sum(1 for a in sub if a[2] >= 10)
    h20 = sum(1 for a in sub if a[2] >= 20)
    mean_g = sum(a[2] for a in sub) / n
    print(f"{thr:>10} {n:>8} {h5}/{n}={h5/n*100:>4.0f}% {h10}/{n}={h10/n*100:>5.0f}% {h20}/{n}={h20/n*100:>5.0f}% {mean_g:>9.2f}")
