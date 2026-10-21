"""Núcleo offline de telemetría. Importarlo no hace I/O ni contactos de red."""

from .model import Metric, Observation, PollAttempt
from .rates import RatePolicy, RateResult, calculate_rate
from .continuity import ContinuityPolicy, TrafficRate, calculate_traffic_rate
from .store import ConflictError, TelemetryStore

__all__ = [
    "Metric", "Observation", "PollAttempt", "RatePolicy", "RateResult",
    "calculate_rate", "ConflictError", "TelemetryStore",
    "ContinuityPolicy", "TrafficRate", "calculate_traffic_rate",
]
