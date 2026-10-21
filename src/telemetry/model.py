"""Contrato v1: tiempos UTC, valores crudos e identidad de inventario explícita."""

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
import math

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def timestamp(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    micros(result)
    return result.astimezone(UTC)


def micros(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Los tiempos deben incluir zona horaria")
    delta = value.astimezone(UTC) - EPOCH
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def from_micros(value: int) -> datetime:
    return EPOCH + timedelta(microseconds=value)


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def require_text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} debe ser texto no vacío")


@dataclass(frozen=True)
class Metric:
    name: str
    kind: str
    unit: str
    counter_bits: int | None = None

    def __post_init__(self):
        require_text(self.name, "metric.name")
        require_text(self.unit, "metric.unit")
        if self.kind not in {"counter", "gauge", "state"}:
            raise ValueError("metric.kind inválido")
        if self.kind == "counter":
            if type(self.counter_bits) is not int or self.counter_bits not in {32, 64}:
                raise ValueError("Un contador requiere counter_bits 32 o 64")
        elif self.counter_bits is not None:
            raise ValueError("Solo un contador tiene ancho de bits")


@dataclass(frozen=True)
class Observation:
    poll_id: str
    device_id: str
    interface_id: str
    interface_identity: str
    metric: Metric
    raw_value: int | float | str | None
    queried_at: datetime
    ingested_at: datetime
    quality: str = "good"
    observed_at: datetime | None = None
    uptime_ticks: int | None = None
    discontinuity_ticks: int | None = None
    interface_index: int | None = None
    interface_name: str | None = None
    schema_version: int = 1

    def __post_init__(self):
        for field in ("poll_id", "device_id", "interface_id", "interface_identity"):
            require_text(getattr(self, field), field)
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("Versión de observación no soportada")
        if not isinstance(self.metric, Metric):
            raise ValueError("metric debe ser Metric")
        if self.quality not in {"good", "missing", "error", "unknown"}:
            raise ValueError("quality inválida")
        if micros(self.ingested_at) < micros(self.queried_at):
            raise ValueError("ingested_at precede a queried_at")
        if self.observed_at is not None:
            micros(self.observed_at)
        for field in ("uptime_ticks", "discontinuity_ticks"):
            value = getattr(self, field)
            if value is not None and (type(value) is not int or not 0 <= value < 2**32):
                raise ValueError(f"{field} debe ser TimeTicks de 32 bits")
        if self.interface_index is not None and (
            type(self.interface_index) is not int or self.interface_index <= 0
        ):
            raise ValueError("interface_index debe ser entero positivo")
        if self.quality != "good":
            if self.raw_value is not None:
                raise ValueError("Una muestra no válida debe tener raw_value=null")
            return
        value = self.raw_value
        if self.metric.kind == "counter":
            if type(value) is not int or not 0 <= value < 2**self.metric.counter_bits:
                raise ValueError("Contador fuera de rango o no entero")
        elif self.metric.kind == "gauge":
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError("Gauge debe ser numérico finito")
        elif type(value) not in (int, str):
            raise ValueError("State debe ser texto o entero")

    def to_dict(self) -> dict:
        result = asdict(self)
        for field in ("observed_at", "queried_at", "ingested_at"):
            value = getattr(self, field)
            result[field] = value.astimezone(UTC).isoformat() if value else None
        return result

    @classmethod
    def from_dict(cls, value: dict):
        data = dict(value)
        data["metric"] = Metric(**data["metric"])
        for field in ("observed_at", "queried_at", "ingested_at"):
            if data.get(field) is not None:
                data[field] = timestamp(data[field])
        return cls(**data)

    @property
    def dedup_key(self) -> str:
        identity = [self.schema_version, self.poll_id, self.device_id,
                    self.interface_id, self.interface_identity, self.metric.name]
        return hashlib.sha256(canonical(identity).encode("utf-8")).hexdigest()

    @property
    def content(self) -> str:
        value = self.to_dict()
        # Reprocesar no cambia el primer tiempo de ingestión persistido.
        value.pop("ingested_at")
        return canonical(value)


@dataclass(frozen=True)
class PollAttempt:
    poll_id: str
    device_id: str
    interface_id: str
    interface_identity: str
    started_at: datetime
    finished_at: datetime
    metrics: tuple[Metric, ...]
    status: str
    error_code: str | None = None

    def __post_init__(self):
        for field in ("poll_id", "device_id", "interface_id", "interface_identity"):
            require_text(getattr(self, field), field)
        if micros(self.finished_at) < micros(self.started_at):
            raise ValueError("El intento termina antes de comenzar")
        if not isinstance(self.metrics, tuple) or not self.metrics:
            raise ValueError("metrics debe ser una tupla no vacía")
        if not all(isinstance(metric, Metric) for metric in self.metrics):
            raise ValueError("Todas las métricas deben ser Metric")
        if len({metric.name for metric in self.metrics}) != len(self.metrics):
            raise ValueError("Métricas esperadas duplicadas")
        if self.status not in {"success", "partial", "error"}:
            raise ValueError("Estado de intento inválido")
        if self.status == "success" and self.error_code is not None:
            raise ValueError("Un intento exitoso no tiene error_code")
        if self.status != "success":
            require_text(self.error_code, "error_code")

    def to_dict(self) -> dict:
        result = asdict(self)
        result["started_at"] = self.started_at.astimezone(UTC).isoformat()
        result["finished_at"] = self.finished_at.astimezone(UTC).isoformat()
        result["metrics"] = sorted(result["metrics"], key=lambda item: item["name"])
        return result

    @classmethod
    def from_dict(cls, value: dict):
        data = dict(value)
        data["metrics"] = tuple(Metric(**item) for item in data["metrics"])
        for field in ("started_at", "finished_at"):
            data[field] = timestamp(data[field])
        return cls(**data)
