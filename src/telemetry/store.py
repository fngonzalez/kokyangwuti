"""SQLite local, lotes finalizados atómicos e histórico con huecos explícitos."""

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import sqlite3

from .model import Observation, PollAttempt, canonical, from_micros, micros
from .rates import RatePolicy, RateResult, calculate_rate


class ConflictError(ValueError):
    """La identidad ya existe con otro contenido; el lote completo se revierte."""


@dataclass(frozen=True)
class IngestResult:
    new_polls: int
    inserted_observations: int
    duplicate_observations: int


SCHEMA = """
BEGIN;
CREATE TABLE polls (
    poll_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    interface_id TEXT NOT NULL,
    finished_us INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('success','partial','error')),
    error_code TEXT,
    payload TEXT NOT NULL
);
CREATE INDEX polls_history ON polls(device_id, interface_id, finished_us, poll_id);
CREATE TABLE poll_metrics (
    poll_id TEXT NOT NULL REFERENCES polls(poll_id),
    metric TEXT NOT NULL,
    spec TEXT NOT NULL,
    PRIMARY KEY(poll_id, metric)
);
CREATE TABLE observations (
    observation_id TEXT PRIMARY KEY,
    poll_id TEXT NOT NULL,
    metric TEXT NOT NULL,
    device_id TEXT NOT NULL,
    interface_id TEXT NOT NULL,
    queried_us INTEGER NOT NULL,
    ingested_us INTEGER NOT NULL,
    quality TEXT NOT NULL CHECK(quality IN ('good','missing','error','unknown')),
    payload TEXT NOT NULL,
    content TEXT NOT NULL,
    UNIQUE(poll_id, metric),
    FOREIGN KEY(poll_id, metric) REFERENCES poll_metrics(poll_id, metric)
);
CREATE INDEX observations_history ON observations(
    device_id, interface_id, metric, queried_us
);
PRAGMA user_version=1;
PRAGMA application_id=0x4b4f4b59;
COMMIT;
"""


class TelemetryStore:
    def __init__(self, path: str | Path | None = None):
        path = str(path if path is not None else os.getenv(
            "KOKYANGWUTI_DB_PATH", "output/telemetry.sqlite3"))
        if path.startswith(("\\\\", "//")):
            raise ValueError("Se requiere una base local, no una ruta UNC")
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=5)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        try:
            version = self.connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                tables = self.connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
                if tables:
                    raise ValueError("Base preexistente sin versión: no se modifica")
                self.connection.executescript(SCHEMA)
            elif version != 1:
                raise ValueError(f"Versión SQLite no soportada: {version}")
            elif self.connection.execute("PRAGMA application_id").fetchone()[0] != 0x4B4F4B59:
                raise ValueError("La base pertenece a otra aplicación")
        except Exception:
            self.connection.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        self.connection.close()

    def ingest(self, attempt: PollAttempt, observations) -> IngestResult:
        """Un poll finalizado de un dispositivo/interfaz; replay idéntico es inocuo."""
        expected = {metric.name: metric for metric in attempt.metrics}
        incoming = {}
        duplicates_in_batch = 0
        for observation in observations:
            if not isinstance(observation, Observation):
                raise ValueError("El lote solo admite Observation")
            for field in ("poll_id", "device_id", "interface_id", "interface_identity"):
                if getattr(observation, field) != getattr(attempt, field):
                    raise ValueError(f"Observación fuera del intento: {field}")
            if expected.get(observation.metric.name) != observation.metric:
                raise ValueError("Métrica no esperada o definición incompatible")
            if not micros(attempt.started_at) <= micros(observation.queried_at) <= micros(attempt.finished_at):
                raise ValueError("queried_at debe estar dentro del intento")
            previous = incoming.get(observation.metric.name)
            if previous is not None:
                if previous.content != observation.content:
                    raise ConflictError("Dos resultados diferentes para la misma métrica")
                duplicates_in_batch += 1
            else:
                incoming[observation.metric.name] = observation
        good = sum(item.quality == "good" for item in incoming.values())
        status = "success" if good == len(expected) else "partial" if good else "error"
        if attempt.status != status:
            raise ValueError("Estado del intento incompatible con sus resultados")
        payload = canonical(attempt.to_dict())
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            existing = self.connection.execute(
                "SELECT payload FROM polls WHERE poll_id=?", (attempt.poll_id,)
            ).fetchone()
            if existing is not None:
                recorded = self.connection.execute(
                    "SELECT metric, content FROM observations WHERE poll_id=?",
                    (attempt.poll_id,),
                ).fetchall()
                if existing["payload"] != payload or {
                    row["metric"]: row["content"] for row in recorded
                } != {name: item.content for name, item in incoming.items()}:
                    raise ConflictError("poll_id reutilizado con contenido diferente")
                return IngestResult(0, 0, len(incoming) + duplicates_in_batch)
            self.connection.execute(
                "INSERT INTO polls VALUES (?,?,?,?,?,?,?)",
                (attempt.poll_id, attempt.device_id, attempt.interface_id,
                 micros(attempt.finished_at), attempt.status, attempt.error_code, payload),
            )
            self.connection.executemany(
                "INSERT INTO poll_metrics VALUES (?,?,?)",
                [(attempt.poll_id, name, canonical(asdict(metric)))
                 for name, metric in expected.items()],
            )
            self.connection.executemany(
                "INSERT INTO observations VALUES (?,?,?,?,?,?,?,?,?,?)",
                [(item.dedup_key, item.poll_id, item.metric.name, item.device_id,
                  item.interface_id, micros(item.queried_at), micros(item.ingested_at),
                  item.quality, canonical(item.to_dict()), item.content)
                 for item in incoming.values()],
            )
        return IngestResult(1, len(incoming), duplicates_in_batch)

    def reconcile(self) -> dict:
        """Unidades explícitas; cuenta intentos lógicos únicos, no replays de ingesta."""
        polls = self.connection.execute("""
            SELECT COUNT(*) AS polls,
                   COALESCE(SUM(status='success'),0) AS polls_success,
                   COALESCE(SUM(status='partial'),0) AS polls_partial,
                   COALESCE(SUM(status='error'),0) AS polls_error
            FROM polls
        """).fetchone()
        result = dict(polls)
        result["metric_slots_expected"] = self.connection.execute(
            "SELECT COUNT(*) FROM poll_metrics"
        ).fetchone()[0]
        rows = self.connection.execute("""
            SELECT COUNT(*) AS observation_rows,
                   COALESCE(SUM(quality='good'),0) AS metric_results_good,
                   COALESCE(SUM(quality!='good'),0) AS metric_results_not_good
            FROM observations
        """).fetchone()
        result.update(dict(rows))
        result["metric_slots_without_row"] = result["metric_slots_expected"] - result["observation_rows"]
        result["metric_slots_without_good_result"] = result["metric_slots_expected"] - result["metric_results_good"]
        return result

    def _timeline(self, device_id, interface_id, metric, start_us, end_us, predecessor=False):
        # Cada rama filtra su columna temporal indexada; COALESCE en WHERE
        # obligaría a recorrer toda la serie para consultar un período corto.
        def bounds(column):
            return f"{column}<?" if predecessor else f"{column}>=? AND {column}<?"

        query = f"""
            SELECT p.poll_id AS poll_id, p.status, p.error_code, p.payload AS poll_payload,
                   o.payload AS observation_payload, pm.spec, o.queried_us AS sample_us
            FROM observations o
            JOIN polls p ON p.poll_id=o.poll_id
            JOIN poll_metrics pm ON pm.poll_id=o.poll_id AND pm.metric=o.metric
            WHERE o.device_id=? AND o.interface_id=? AND o.metric=?
              AND {bounds('o.queried_us')}
            UNION ALL
            SELECT p.poll_id AS poll_id, p.status, p.error_code, p.payload AS poll_payload,
                   NULL AS observation_payload, pm.spec, p.finished_us AS sample_us
            FROM polls p JOIN poll_metrics pm ON pm.poll_id=p.poll_id
            WHERE p.device_id=? AND p.interface_id=? AND pm.metric=?
              AND {bounds('p.finished_us')}
              AND NOT EXISTS (SELECT 1 FROM observations o
                              WHERE o.poll_id=p.poll_id AND o.metric=pm.metric)
        """
        query += " ORDER BY sample_us DESC,poll_id DESC LIMIT 1" if predecessor else " ORDER BY sample_us,poll_id"
        times = (start_us,) if predecessor else (start_us, end_us)
        args = (device_id, interface_id, metric) + times
        return self.connection.execute(query, args + args).fetchall()

    def history(self, device_id: str, interface_id: str, metric: str,
                start: datetime, end: datetime, policy: RatePolicy,
                as_of: datetime | None = None) -> list[dict]:
        """Ventana [start,end); una muestra anterior es contexto, no salida adicional."""
        start_us, end_us = micros(start), micros(end)
        if end_us <= start_us:
            raise ValueError("El período debe tener duración positiva")
        as_of = as_of if as_of is not None else datetime.now(UTC)
        now_us = micros(as_of)
        if end_us > now_us:
            raise ValueError("end no puede estar después de as_of")
        context = self._timeline(device_id, interface_id, metric, start_us, end_us, predecessor=True)
        predecessor = context[0] if context else None
        rows = self._timeline(device_id, interface_id, metric, start_us, end_us)
        expected_us = round(policy.expected_interval_seconds * 1_000_000)
        max_us = round(policy.max_interval_seconds * 1_000_000)
        output = []

        def gap(left, right):
            left, right = max(start_us, left), min(end_us, right)
            if left < right:
                output.append({"kind": "gap", "start": from_micros(left).isoformat(),
                               "end": from_micros(right).isoformat(),
                               "reason": "no_poll_recorded", "raw_value": None,
                               "rate": None})

        previous = (Observation.from_dict(json.loads(predecessor["observation_payload"]))
                    if predecessor and predecessor["observation_payload"] else None)
        previous_us = predecessor["sample_us"] if predecessor else None
        for row in rows:
            sample_us = row["sample_us"]
            if previous_us is None:
                if sample_us - start_us > max_us:
                    gap(start_us, sample_us)
            elif sample_us - previous_us > max_us:
                gap(previous_us + expected_us, sample_us)
            observation = (Observation.from_dict(json.loads(row["observation_payload"]))
                           if row["observation_payload"] else None)
            if observation is None:
                rate = RateResult(None, None, "missing_observation")
            else:
                rate = calculate_rate(previous, observation, policy)
                if previous is None and previous_us is not None and observation.quality == "good":
                    rate = RateResult(None, None, "previous_poll_missing")
            age = (now_us - sample_us) / 1_000_000
            freshness = ("stale" if age > policy.stale_after_seconds else "fresh")
            quality = observation.quality if observation else "missing"
            output.append({
                "kind": "observation" if observation else "missing",
                "poll_id": row["poll_id"], "poll_status": row["status"],
                "error_code": row["error_code"],
                "device_id": device_id, "interface_id": interface_id,
                "interface_identity": json.loads(row["poll_payload"])["interface_identity"],
                "metric": json.loads(row["spec"]),
                "queried_at": from_micros(sample_us).isoformat(),
                "observed_at": observation.to_dict()["observed_at"] if observation else None,
                "ingested_at": observation.to_dict()["ingested_at"] if observation else None,
                "raw_value": observation.raw_value if observation else None,
                "schema_version": observation.schema_version if observation else None,
                "dedup_key": observation.dedup_key if observation else None,
                "uptime_ticks": observation.uptime_ticks if observation else None,
                "discontinuity_ticks": observation.discontinuity_ticks if observation else None,
                "interface_index": observation.interface_index if observation else None,
                "interface_name": observation.interface_name if observation else None,
                "quality": quality, "age_seconds": age, "freshness": freshness,
                "current_state": "unknown" if freshness == "stale" or quality != "good" else "known",
                "rate": asdict(rate),
            })
            previous, previous_us = observation, sample_us
        if previous_us is None:
            gap(start_us, end_us)
        elif end_us - previous_us > max_us:
            gap(previous_us + expected_us, end_us)
        return output
