"""CLI offline: ingesta de fixtures JSON e histórico; nunca consulta dispositivos."""

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3

from .model import Observation, PollAttempt, timestamp
from .rates import RatePolicy
from .continuity import ContinuityPolicy
from .store import TelemetryStore


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    ingest = commands.add_parser("ingest", help="Importar lotes de un fixture local")
    ingest.add_argument("fixture", type=Path)
    history = commands.add_parser("history", help="Histórico en un período [start,end)")
    latest = commands.add_parser("latest", help="Último resultado, con antigüedad y estado desconocido si vence")
    for name in ("start", "end"):
        history.add_argument("--" + name, required=True)
    history.add_argument("--continuity-policy", choices=("strict", "infer-64"), default="strict",
                         help="strict reproduce v1; infer-64 activa la política auditable v2")
    for command in (history, latest):
        for name in ("device", "interface", "metric", "as-of"):
            command.add_argument("--" + name, required=True)
        for name, default in (("expected-interval", 30), ("max-interval", 45),
                              ("min-interval", None), ("stale-after", 90),
                              ("uptime-wrap-tolerance", 2)):
            command.add_argument("--" + name, type=float, default=default)
        command.add_argument("--max-counter-rate", type=float)
    for command in (ingest, history, latest):
        command.add_argument("--database", help="O KOKYANGWUTI_DB_PATH; defecto output/telemetry.sqlite3")
    args = parser.parse_args(argv)
    try:
        if args.command == "ingest":
            if str(args.fixture).startswith(("\\\\", "//")):
                raise ValueError("El fixture debe ser local")
            fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
            if fixture["schema_version"] != 1:
                raise ValueError("Versión de fixture no soportada")
            # Deserializar antes de abrir; la unidad atómica de persistencia es cada poll.
            batches = [(PollAttempt.from_dict(batch["attempt"]),
                        [Observation.from_dict(item) for item in batch["observations"]])
                       for batch in fixture["batches"]]
            with TelemetryStore(args.database) as store:
                results = [asdict(store.ingest(attempt, items)) for attempt, items in batches]
                output = {"ingestion": results, "reconciliation": store.reconcile()}
        else:
            inferred = args.command == "latest" or args.continuity_policy == "infer-64"
            minimum = args.min_interval if args.min_interval is not None else 15 if inferred else 1
            thresholds = dict(expected_interval_seconds=args.expected_interval,
                              max_interval_seconds=args.max_interval,
                              min_interval_seconds=minimum, stale_after_seconds=args.stale_after,
                              max_counter_rate=args.max_counter_rate)
            policy = (ContinuityPolicy(**thresholds, uptime_wrap_tolerance_seconds=args.uptime_wrap_tolerance)
                      if inferred else RatePolicy(**thresholds))
            with TelemetryStore(args.database, read_only=True) as store:
                if args.command == "latest":
                    output = store.latest(args.device, args.interface, args.metric, policy,
                                          timestamp(args.as_of))
                else:
                    output = store.history(args.device, args.interface, args.metric,
                                           timestamp(args.start), timestamp(args.end), policy,
                                           timestamp(args.as_of))
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
        parser.exit(2, f"Error: {error}\n")
    print(json.dumps(output, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
