"""Derivación conservadora: nunca rellena ausencias ni conecta identidades distintas."""

from dataclasses import dataclass
import math

from .model import Observation, micros


@dataclass(frozen=True)
class RatePolicy:
    expected_interval_seconds: float = 30
    max_interval_seconds: float = 45
    min_interval_seconds: float = 1
    stale_after_seconds: float = 90
    max_counter_rate: float | None = None

    def __post_init__(self):
        values = (self.expected_interval_seconds, self.max_interval_seconds,
                  self.min_interval_seconds, self.stale_after_seconds)
        if any(type(value) not in (int, float) or not math.isfinite(value)
               or value <= 0 for value in values):
            raise ValueError("Intervalos y antigüedad deben ser positivos y finitos")
        if not self.min_interval_seconds <= self.expected_interval_seconds <= self.max_interval_seconds:
            raise ValueError("Se requiere min_interval <= expected_interval <= max_interval")
        if self.max_counter_rate is not None and (
            type(self.max_counter_rate) not in (int, float)
            or not math.isfinite(self.max_counter_rate) or self.max_counter_rate <= 0
        ):
            raise ValueError("max_counter_rate debe ser positivo y finito")


@dataclass(frozen=True)
class RateResult:
    value: float | None
    unit: str | None
    reason: str
    elapsed_seconds: float | None = None
    delta: int | None = None
    wrapped: bool = False
    bits_per_second: float | None = None


def calculate_rate(previous: Observation | None, current: Observation,
                   policy: RatePolicy) -> RateResult:
    def unavailable(reason, elapsed=None):
        return RateResult(None, None, reason, elapsed)

    if current.quality != "good":
        return unavailable("current_sample_" + current.quality)
    if current.metric.kind != "counter":
        return unavailable("not_a_counter")
    if previous is None:
        return unavailable("no_previous_sample")
    if (previous.device_id, previous.interface_id, previous.interface_identity) != (
        current.device_id, current.interface_id, current.interface_identity
    ):
        return unavailable("interface_identity_changed")
    if previous.metric != current.metric:
        return unavailable("counter_definition_changed")
    if previous.quality != "good":
        return unavailable("previous_sample_" + previous.quality)
    elapsed_us = micros(current.queried_at) - micros(previous.queried_at)
    elapsed = elapsed_us / 1_000_000
    if elapsed <= 0:
        return unavailable("non_positive_interval", elapsed)
    if elapsed < policy.min_interval_seconds:
        return unavailable("interval_too_short", elapsed)
    if previous.uptime_ticks is not None and current.uptime_ticks is not None:
        if current.uptime_ticks < previous.uptime_ticks:
            return unavailable("uptime_regressed", elapsed)
    else:
        return unavailable("uptime_missing", elapsed)
    if previous.discontinuity_ticks is None or current.discontinuity_ticks is None:
        return unavailable("discontinuity_unknown", elapsed)
    if previous.discontinuity_ticks != current.discontinuity_ticks:
        return unavailable("counter_discontinuity", elapsed)
    if elapsed > policy.max_interval_seconds:
        return unavailable("interval_too_long", elapsed)
    if policy.max_counter_rate is None:
        return unavailable("counter_rate_bound_missing", elapsed)
    modulus = 2**current.metric.counter_bits
    # Si la cota permite una vuelta entera, hasta un delta positivo es ambiguo.
    bound = policy.max_counter_rate * elapsed
    if bound >= modulus:
        return unavailable("multiple_wraps_possible", elapsed)
    wrapped = current.raw_value < previous.raw_value
    delta = (current.raw_value - previous.raw_value) % modulus
    if delta > bound:
        return unavailable("counter_delta_exceeds_bound", elapsed)
    value = delta * 1_000_000 / elapsed_us
    return RateResult(value, current.metric.unit + "/s", "wrap" if wrapped else "ok",
                      elapsed, delta, wrapped,
                      value * 8 if current.metric.unit == "octets" else None)
