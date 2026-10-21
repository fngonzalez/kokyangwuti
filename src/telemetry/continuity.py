"""Política pura y auditable de tasas de tráfico; no tiene I/O."""

from dataclasses import asdict, dataclass
from fractions import Fraction
import hashlib
import math

from .model import Observation, canonical, micros
from .rates import RatePolicy


@dataclass(frozen=True)
class ContinuityPolicy(RatePolicy):
    """Defaults candidatos: nominal 30 s, ventana inclusiva 15–45 s, edad 90 s."""

    min_interval_seconds: float = 15
    uptime_wrap_tolerance_seconds: float = 2

    def __post_init__(self):
        super().__post_init__()
        tolerance = self.uptime_wrap_tolerance_seconds
        if type(tolerance) not in (int, float) or not math.isfinite(tolerance) or not 0 <= tolerance <= self.max_interval_seconds:
            raise ValueError("La tolerancia de uptime debe ser finita y estar entre 0 y el intervalo máximo")

    def snapshot(self):
        return {"algorithm": "traffic-continuity-v1", **asdict(self)}

    @property
    def policy_id(self):
        return hashlib.sha256(canonical(self.snapshot()).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TrafficRate:
    value_bps: float | None
    state: str
    reason: str
    previous_sample: dict | None
    current_sample: dict | None
    elapsed_seconds: float | None
    policy: dict
    policy_id: str
    schema_version: int = 2
    unit: str = "bits/s"
    time_basis: str = "queried_at"
    wrapped: bool = False


def _source(sample):
    return {"observation_id": sample.dedup_key, **sample.to_dict()} if sample else None


def calculate_traffic_rate(previous: Observation | None, current: Observation | None,
                           policy: ContinuityPolicy) -> TrafficRate:
    """Analiza solo el par recibido; el llamador debe entregar muestras consecutivas.

    Marca comparable sin cambios permite `calculada`. Sin ella solo se infiere
    Counter64 no decreciente. Nunca inventa la marca ni saltea una muestra ausente.
    """
    elapsed_us = (micros(current.queried_at) - micros(previous.queried_at)
                  if previous is not None and current is not None else None)
    elapsed = elapsed_us / 1_000_000 if elapsed_us is not None else None

    def result(reason, state="no_calculable", value=None, wrapped=False):
        return TrafficRate(value, state, reason, _source(previous), _source(current),
                           elapsed, policy.snapshot(), policy.policy_id, wrapped=wrapped)

    if current is None:
        return result("muestra_ausente")
    if previous is None:
        return result("muestra_ausente" if current.quality == "missing" else "sin_muestra_anterior")
    if (previous.device_id, previous.interface_id, previous.interface_identity) != (
        current.device_id, current.interface_id, current.interface_identity
    ):
        return result("identidad_cambiada")
    if previous.quality == "missing" or current.quality == "missing":
        return result("muestra_ausente")
    if previous.quality != "good" or current.quality != "good":
        return result("calidad_no_aceptable")
    if previous.metric != current.metric:
        return result("definicion_contador_cambiada")
    if current.metric.kind != "counter" or current.metric.unit != "octets":
        return result("metrica_no_es_trafico_en_octetos")
    if not policy.min_interval_seconds <= elapsed <= policy.max_interval_seconds:
        return result("intervalo_anormal")
    if previous.uptime_ticks is None or current.uptime_ticks is None:
        return result("uptime_ausente")
    if current.uptime_ticks < previous.uptime_ticks:
        modular_seconds = ((current.uptime_ticks - previous.uptime_ticks) % 2**32) / 100
        if abs(modular_seconds - elapsed) <= policy.uptime_wrap_tolerance_seconds:
            return result("wrap_uptime_ambiguo")
        return result("reinicio_agente")
    marked = previous.discontinuity_ticks is not None and current.discontinuity_ticks is not None
    if marked and previous.discontinuity_ticks != current.discontinuity_ticks:
        return result("discontinuidad_contador")
    decreased = current.raw_value < previous.raw_value
    if not marked:
        if current.metric.counter_bits != 64:
            return result("inferencia_requiere_64_bits")
        if decreased:
            return result("contador_decreciente")
    modulus = 2**current.metric.counter_bits
    if policy.max_counter_rate is None:
        if decreased or current.metric.counter_bits == 32:
            return result("cota_requerida_para_wrap")
    else:
        # Cota exacta respecto de la precisión de consulta; sin redondear Counter64.
        bound = Fraction(str(policy.max_counter_rate)) * Fraction(elapsed_us, 1_000_000)
        if bound >= modulus:
            return result("multiples_wraps_posibles")
    delta = (current.raw_value - previous.raw_value) % modulus
    if policy.max_counter_rate is not None and delta > bound:
        return result("delta_excede_cota")
    value = delta * 8 * 1_000_000 / elapsed_us
    if marked:
        return result("wrap_contador_confirmado" if decreased else "marca_sin_cambios",
                      "calculada", value, wrapped=decreased)
    return result("sin_marca_discontinuidad", "inferida", value)
