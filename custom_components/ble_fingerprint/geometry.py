"""Motore geometrico: posizione dei proxy, trilaterazione e filtro di Kalman.

Tutto in Python puro (nessuna dipendenza da numpy/scipy) e in metri.

Parte delle idee viene da BPS - BLE Positioning System
(https://github.com/Hogster/BPS, licenza MIT, Copyright (c) 2025 Hogster):
trilaterazione ai minimi quadrati pesata 1/r², attenuazione morbida dei salti
di distanza, fit limitato all'area della mappa, punteggio di qualità basato
su residuo e copertura, filtro di Kalman a velocità costante, correzione
moltiplicativa per proxy con perdita asimmetrica (i muri allungano soltanto).

Differenza principale rispetto a BPS: la posizione e la correzione di ogni
proxy non vanno disegnate su una planimetria né richiedono iBeacon sui proxy,
ma vengono stimate dai vertici della mappa registrati con coordinate, usando
le distanze misurate dal telefono stesso.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
import math

# --- Stima dei proxy ---
# Vertici minimi (con coordinate) che vedono un proxy per stimarne la posizione
MIN_PROXY_SAMPLES = 4
# Dispersione minima dei vertici (deviazione standard sull'asse più stretto, m)
MIN_PROXY_SPREAD_M = 0.5
# Residuo massimo (m) perché la stima di un proxy sia considerata affidabile
MAX_PROXY_RMS_M = 2.0
# Limiti del fattore di correzione (distanza misurata / distanza reale)
FACTOR_MIN = 0.3
FACTOR_MAX = 3.0
# Peso dei residui "misura più lunga del previsto" (muri): costa poco lasciarli
# inspiegati, mentre una misura più corta del reale è fisicamente improbabile
WALL_SIDE_WEIGHT = 0.25

# --- Trilaterazione ---
MIN_TRILATERATION_PROXIES = 3
# Raggio minimo (m) usato nel peso 1/r², perché un proxy a 0 m non decida tutto
MIN_WEIGHT_RADIUS_M = 0.5
# Variazione relativa di distanza oltre la quale una lettura viene attenuata
RADIUS_JUMP_TOL = 0.5
# Residuo (m) a cui la qualità del fit vale 0,5
RESIDUAL_SCALE_M = 2.0
# Proxy che rilevano il dispositivo oltre i quali la copertura è piena
COVERAGE_TARGET_N = 5.0
# Margine attorno all'area della mappa entro cui può cadere la posizione (m)
BOUNDS_MARGIN_M = 1.0

# --- Filtro di Kalman (BPS) ---
KF_MEAS_NOISE_M = 1.5
KF_ACCEL_NOISE_MS2 = 0.5
KF_INIT_VEL_UNC_MS = 1.0
KF_MAX_DT_S = 10.0
KF_MAX_GAP_S = 30.0

Point = tuple[float, float]
Bounds = tuple[float, float, float, float]


@dataclass(frozen=True)
class ProxyEstimate:
    """Posizione stimata (o impostata) di un proxy."""

    x: float
    y: float
    factor: float
    rms: float | None
    samples: int
    source: str  # "estimated" | "manual"

    def as_dict(self) -> dict:
        return {
            "x": round(self.x, 2),
            "y": round(self.y, 2),
            "factor": round(self.factor, 3),
            "rms": None if self.rms is None else round(self.rms, 2),
            "samples": self.samples,
            "source": self.source,
        }


@dataclass(frozen=True)
class TrilaterationFix:
    """Risultato della trilaterazione."""

    x: float
    y: float
    rms: float
    proxies: int
    quality: float


# ---------------------------------------------------------------------------
# Algebra lineare minima
# ---------------------------------------------------------------------------


def _solve(matrix: list[list[float]], vector: list[float]) -> list[float] | None:
    """Risolve un piccolo sistema lineare (eliminazione di Gauss con pivot)."""
    n = len(vector)
    aug = [list(row) + [vector[i]] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(aug[r][col]))
        if abs(aug[pivot][col]) < 1e-12:
            return None
        aug[col], aug[pivot] = aug[pivot], aug[col]
        for row in range(col + 1, n):
            ratio = aug[row][col] / aug[col][col]
            for k in range(col, n + 1):
                aug[row][k] -= ratio * aug[col][k]
    result = [0.0] * n
    for row in range(n - 1, -1, -1):
        acc = aug[row][n] - sum(aug[row][k] * result[k] for k in range(row + 1, n))
        result[row] = acc / aug[row][row]
    return result


def _levenberg_marquardt(
    params: list[float],
    residuals_and_jacobian,
    *,
    clamp=None,
    iterations: int = 60,
) -> tuple[list[float], float]:
    """Minimi quadrati non lineari (Levenberg-Marquardt) per pochi parametri.

    `residuals_and_jacobian(params)` restituisce (residui, jacobiano).
    `clamp(params)` riporta i parametri nei limiti ammessi.
    Restituisce (parametri, somma dei quadrati dei residui).
    """
    damping = 1e-3
    res, jac = residuals_and_jacobian(params)
    cost = sum(r * r for r in res)
    n = len(params)

    for _ in range(iterations):
        jtj = [[sum(j[a] * j[b] for j in jac) for b in range(n)] for a in range(n)]
        jtr = [sum(j[a] * r for j, r in zip(jac, res, strict=True)) for a in range(n)]
        for a in range(n):
            jtj[a][a] += damping * (jtj[a][a] + 1e-9)
        step = _solve(jtj, [-v for v in jtr])
        if step is None:
            break
        candidate = [p + s for p, s in zip(params, step, strict=True)]
        if clamp is not None:
            candidate = clamp(candidate)
        new_res, new_jac = residuals_and_jacobian(candidate)
        new_cost = sum(r * r for r in new_res)
        if new_cost < cost:
            moved = max(abs(c - p) for c, p in zip(candidate, params, strict=True))
            params, res, jac, cost = candidate, new_res, new_jac, new_cost
            damping = max(damping / 3, 1e-9)
            if moved < 1e-5:
                break
        else:
            damping *= 4
            if damping > 1e8:
                break

    return params, cost


# ---------------------------------------------------------------------------
# Stima della posizione dei proxy dai vertici della mappa
# ---------------------------------------------------------------------------


def _min_spread(points: Sequence[Point]) -> float:
    """Deviazione standard lungo la direzione di minima dispersione (m)."""
    n = len(points)
    mx = sum(p[0] for p in points) / n
    my = sum(p[1] for p in points) / n
    sxx = sum((p[0] - mx) ** 2 for p in points) / n
    syy = sum((p[1] - my) ** 2 for p in points) / n
    sxy = sum((p[0] - mx) * (p[1] - my) for p in points) / n
    trace, det = sxx + syy, sxx * syy - sxy * sxy
    disc = math.sqrt(max(trace * trace / 4 - det, 0.0))
    return math.sqrt(max(trace / 2 - disc, 0.0))


def fit_proxy(samples: Sequence[tuple[float, float, float]]) -> ProxyEstimate | None:
    """Stima posizione e fattore di correzione di un proxy.

    `samples` sono terne (x, y, distanza misurata) dei vertici che vedono il
    proxy. Il modello è `misurata ≈ fattore × reale`: il fattore assorbe
    l'attenuazione propria di quel proxy (scatola a muro, antenna, potenza).
    """
    if len(samples) < MIN_PROXY_SAMPLES:
        return None
    if _min_spread([(s[0], s[1]) for s in samples]) < MIN_PROXY_SPREAD_M:
        return None

    # Punto di partenza: media dei vertici pesata verso quelli più vicini
    weights = [1.0 / (d + 0.5) ** 2 for _, _, d in samples]
    total = sum(weights)
    px = sum(w * s[0] for w, s in zip(weights, samples, strict=True)) / total
    py = sum(w * s[1] for w, s in zip(weights, samples, strict=True)) / total
    ratios = sorted(
        d / max(math.hypot(x - px, y - py), 0.3) for x, y, d in samples
    )
    factor = min(max(ratios[len(ratios) // 2], FACTOR_MIN), FACTOR_MAX)

    def evaluate(params):
        qx, qy, k = params
        res, jac = [], []
        for x, y, d in samples:
            dist = max(math.hypot(qx - x, qy - y), 1e-6)
            r = d - k * dist
            # perdita asimmetrica: misura lunga (muro) pesa meno
            w = math.sqrt(WALL_SIDE_WEIGHT if r > 0 else 1.0)
            res.append(w * r)
            jac.append([
                -w * k * (qx - x) / dist,
                -w * k * (qy - y) / dist,
                -w * dist,
            ])
        return res, jac

    def clamp(params):
        return [params[0], params[1], min(max(params[2], FACTOR_MIN), FACTOR_MAX)]

    (qx, qy, k), _ = _levenberg_marquardt([px, py, factor], evaluate, clamp=clamp)

    # residuo non pesato, nella scala delle distanze reali
    rms = math.sqrt(
        sum((d / k - math.hypot(qx - x, qy - y)) ** 2 for x, y, d in samples)
        / len(samples)
    )
    if rms > MAX_PROXY_RMS_M:
        return None

    return ProxyEstimate(
        x=qx, y=qy, factor=k, rms=rms, samples=len(samples), source="estimated"
    )


def estimate_proxies(
    vertices: Iterable[tuple[float, float, dict[str, float]]],
    overrides: dict[str, dict] | None = None,
) -> dict[str, ProxyEstimate]:
    """Stima tutti i proxy visti da almeno MIN_PROXY_SAMPLES vertici.

    `vertices` sono terne (x, y, {proxy: distanza}). Le posizioni impostate a
    mano in `overrides` ({proxy: {"x", "y", "factor"?}}) hanno la precedenza.
    """
    samples: dict[str, list[tuple[float, float, float]]] = {}
    for x, y, distances in vertices:
        for proxy, distance in distances.items():
            samples.setdefault(proxy, []).append((x, y, distance))

    proxies: dict[str, ProxyEstimate] = {}
    for proxy, proxy_samples in samples.items():
        if (estimate := fit_proxy(proxy_samples)) is not None:
            proxies[proxy] = estimate

    for proxy, data in (overrides or {}).items():
        try:
            x, y = float(data["x"]), float(data["y"])
        except (KeyError, TypeError, ValueError):
            continue
        fitted = proxies.get(proxy)
        factor = data.get("factor")
        if factor is None:
            # mantieni la correzione stimata se c'è
            factor = fitted.factor if fitted else 1.0
        proxies[proxy] = ProxyEstimate(
            x=x,
            y=y,
            factor=min(max(float(factor), FACTOR_MIN), FACTOR_MAX),
            rms=None,
            samples=len(samples.get(proxy, [])),
            source="manual",
        )

    return proxies


def map_bounds(points: Iterable[Point], margin: float = BOUNDS_MARGIN_M) -> Bounds | None:
    """Rettangolo che contiene i punti, allargato di `margin` metri."""
    points = list(points)
    if not points:
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (min(xs) - margin, min(ys) - margin, max(xs) + margin, max(ys) + margin)


def _clip(point: Point, bounds: Bounds | None) -> Point:
    if bounds is None:
        return point
    minx, miny, maxx, maxy = bounds
    return (min(max(point[0], minx), maxx), min(max(point[1], miny), maxy))


# ---------------------------------------------------------------------------
# Trilaterazione
# ---------------------------------------------------------------------------


def jump_weight(radius: float, previous: float | None) -> float:
    """Attenua (senza scartarla) una distanza cambiata bruscamente (BPS)."""
    if previous is None:
        return 1.0
    r_eff = max(radius, MIN_WEIGHT_RADIUS_M)
    p_eff = max(previous, MIN_WEIGHT_RADIUS_M)
    rel = max(r_eff / p_eff, p_eff / r_eff)
    return 1.0 / (1.0 + ((rel - 1.0) / RADIUS_JUMP_TOL) ** 2)


def trilaterate(
    circles: Sequence[tuple[float, float, float, float]],
    *,
    start: Point | None = None,
    bounds: Bounds | None = None,
) -> TrilaterationFix | None:
    """Minimi quadrati pesati sui cerchi (x, y, raggio, affidabilità).

    Peso = affidabilità / max(raggio, MIN_WEIGHT_RADIUS_M)²: i proxy vicini,
    le cui misure sono più precise, contano di più. La soluzione è limitata a
    `bounds`.
    """
    if len(circles) < MIN_TRILATERATION_PROXIES:
        return None

    weights = [w / max(r, MIN_WEIGHT_RADIUS_M) ** 2 for _, _, r, w in circles]
    if sum(weights) <= 0:
        return None

    if start is None:
        total = sum(weights)
        start = (
            sum(w * c[0] for w, c in zip(weights, circles, strict=True)) / total,
            sum(w * c[1] for w, c in zip(weights, circles, strict=True)) / total,
        )
    start = _clip(start, bounds)

    def evaluate(params):
        qx, qy = params
        res, jac = [], []
        for (x, y, r, _), w in zip(circles, weights, strict=True):
            dist = max(math.hypot(qx - x, qy - y), 1e-6)
            sw = math.sqrt(w)
            res.append(sw * (dist - r))
            jac.append([sw * (qx - x) / dist, sw * (qy - y) / dist])
        return res, jac

    def clamp(params):
        return list(_clip((params[0], params[1]), bounds))

    (qx, qy), _ = _levenberg_marquardt(list(start), evaluate, clamp=clamp)

    # residuo pesato per l'affidabilità (non per 1/r²), in metri, corretto per
    # i gradi di libertà: con 3 proxy il fit è quasi sempre perfetto (BPS)
    num = den = 0.0
    for x, y, r, w in circles:
        num += w * (math.hypot(qx - x, qy - y) - r) ** 2
        den += w
    n = len(circles)
    rms = math.sqrt(num / den) * math.sqrt(n / max(n - 2.0, 1.0)) if den > 0 else 99.0

    quality = 0.5 * min(1.0, n / COVERAGE_TARGET_N) + 0.5 / (
        1.0 + (rms / RESIDUAL_SCALE_M) ** 2
    )
    return TrilaterationFix(x=qx, y=qy, rms=rms, proxies=n, quality=quality)


def fingerprint_quality(score: float) -> float:
    """Qualità (0-1) di una stima fingerprint dallo scarto del vertice migliore."""
    return 1.0 / (1.0 + (score / RESIDUAL_SCALE_M) ** 2)


def fuse(
    fingerprint: tuple[Point, float] | None,
    trilateration: tuple[Point, float] | None,
) -> tuple[Point, float, str] | None:
    """Media pesata (per qualità) delle due stime: (punto, qualità, metodo)."""
    estimates = [
        (point, quality, name)
        for item, name in ((fingerprint, "fingerprint"), (trilateration, "trilateration"))
        if item is not None
        for point, quality in [item]
    ]
    if not estimates:
        return None
    if len(estimates) == 1:
        return estimates[0]

    total = sum(q for _, q, _ in estimates)
    if total <= 0:
        return None
    x = sum(p[0] * q for p, q, _ in estimates) / total
    y = sum(p[1] * q for p, q, _ in estimates) / total
    quality = max(q for _, q, _ in estimates)
    return (x, y), quality, "combined"


# ---------------------------------------------------------------------------
# Filtro di Kalman a velocità costante (per asse, equivalente al 4x4 di BPS)
# ---------------------------------------------------------------------------


class _AxisKalman:
    """Kalman posizione/velocità su un asse."""

    def __init__(self, position: float) -> None:
        r = KF_MEAS_NOISE_M**2
        self.x = [position, 0.0]
        self.p = [[r, 0.0], [0.0, KF_INIT_VEL_UNC_MS**2]]

    def update(self, measurement: float, dt: float) -> float:
        a = KF_ACCEL_NOISE_MS2**2
        r = KF_MEAS_NOISE_M**2
        (p00, p01), (p10, p11) = self.p

        # predizione
        pos = self.x[0] + dt * self.x[1]
        vel = self.x[1]
        q00, q01, q11 = a * dt**4 / 4, a * dt**3 / 2, a * dt**2
        n00 = p00 + dt * (p10 + p01) + dt * dt * p11 + q00
        n01 = p01 + dt * p11 + q01
        n10 = p10 + dt * p11 + q01
        n11 = p11 + q11

        # aggiornamento
        s = n00 + r
        k0, k1 = n00 / s, n10 / s
        innovation = measurement - pos
        self.x = [pos + k0 * innovation, vel + k1 * innovation]
        self.p = [
            [(1 - k0) * n00, (1 - k0) * n01],
            [n10 - k1 * n00, n11 - k1 * n01],
        ]
        return self.x[0]


class PositionFilter:
    """Filtro di Kalman 2D; si azzera dopo un'assenza o un cambio di piano."""

    def __init__(self) -> None:
        self._axes: tuple[_AxisKalman, _AxisKalman] | None = None
        self._last: float | None = None
        self._floor: str | None = None

    def reset(self) -> None:
        self._axes = None
        self._last = None

    def update(self, point: Point, timestamp: float, floor: str | None = None) -> Point:
        if (
            self._axes is None
            or self._last is None
            or floor != self._floor
            or timestamp - self._last > KF_MAX_GAP_S
        ):
            self._axes = (_AxisKalman(point[0]), _AxisKalman(point[1]))
            self._last = timestamp
            self._floor = floor
            return point

        dt = min(max(timestamp - self._last, 1e-3), KF_MAX_DT_S)
        self._last = timestamp
        return (self._axes[0].update(point[0], dt), self._axes[1].update(point[1], dt))


# ---------------------------------------------------------------------------
# Stanze disegnate (poligoni)
# ---------------------------------------------------------------------------


def point_in_polygon(point: Point, polygon: Sequence[Point]) -> bool:
    """True se il punto è dentro il poligono (ray casting)."""
    x, y = point
    inside = False
    n = len(polygon)
    for i in range(n):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            cross = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < cross:
                inside = not inside
    return inside


def nearest_point_on_polygon(point: Point, polygon: Sequence[Point]) -> tuple[Point, float]:
    """Punto del perimetro più vicino e relativa distanza."""
    px, py = point
    best: Point = polygon[0]
    best_dist = math.inf
    n = len(polygon)
    for i in range(n):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % n]
        dx, dy = x2 - x1, y2 - y1
        length2 = dx * dx + dy * dy
        t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / length2))
        cx, cy = x1 + t * dx, y1 + t * dy
        dist = math.hypot(px - cx, py - cy)
        if dist < best_dist:
            best, best_dist = (cx, cy), dist
    return best, best_dist


def polygon_centroid(polygon: Sequence[Point]) -> Point:
    """Baricentro dell'area del poligono (media dei vertici se degenere)."""
    area = cx = cy = 0.0
    n = len(polygon)
    for i in range(n):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % n]
        cross = x1 * y2 - x2 * y1
        area += cross
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    if abs(area) < 1e-9:
        return (sum(p[0] for p in polygon) / n, sum(p[1] for p in polygon) / n)
    area *= 0.5
    return (cx / (6 * area), cy / (6 * area))


def locate_in_zones(
    point: Point, zones: Sequence[tuple[str, Sequence[Point]]]
) -> tuple[str, Point] | None:
    """Stanza che contiene il punto; se nessuna, la più vicina (BPS).

    Restituisce (stanza, punto), dove il punto è agganciato dentro la stanza
    più vicina quando cade fuori da tutte (ad esempio dentro un muro).
    """
    candidates = [(name, poly) for name, poly in zones if len(poly) >= 3]
    if not candidates:
        return None
    for name, polygon in candidates:
        if point_in_polygon(point, polygon):
            return name, point

    best: tuple[str, Point] | None = None
    best_dist = math.inf
    for name, polygon in candidates:
        snapped, dist = nearest_point_on_polygon(point, polygon)
        if dist < best_dist:
            # spinge il punto appena dentro il perimetro, verso il centro
            cx, cy = polygon_centroid(polygon)
            sx = snapped[0] + (cx - snapped[0]) * 0.02
            sy = snapped[1] + (cy - snapped[1]) * 0.02
            best, best_dist = (name, (sx, sy)), dist
    return best


def guided_points(
    polygon: Sequence[Point], inset: float
) -> list[tuple[str, int, Point]]:
    """Punti della calibrazione guidata: centro e angoli rientrati di `inset`.

    Gli angoli vengono spostati verso il centro della stanza, così il punto da
    registrare non è dentro il muro.
    """
    cx, cy = polygon_centroid(polygon)
    points: list[tuple[str, int, Point]] = [("center", 0, (cx, cy))]
    for index, (x, y) in enumerate(polygon, start=1):
        dist = math.hypot(cx - x, cy - y)
        if dist <= 1e-9:
            continue
        step = min(inset, dist * 0.5)
        points.append(
            ("corner", index, (x + (cx - x) * step / dist, y + (cy - y) * step / dist))
        )
    return points
