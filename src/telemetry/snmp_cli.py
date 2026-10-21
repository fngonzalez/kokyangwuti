"""Adquisición SNMP v2c finita: inventory, inspect y poll de una sola interfaz."""

import argparse
import asyncio
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
import json
import math
import sqlite3

from .continuity import ContinuityPolicy
from .snmp import PySnmpClient, SnmpFailure, SnmpSettings, capture_once, inspect_interface, inventory
from .store import TelemetryStore


async def run(settings, args, client_factory=PySnmpClient):
    if args.command == "poll":
        settings.require_binding()
        if type(args.count) is not int or not 1 <= args.count <= 120:
            raise ValueError("count debe estar entre 1 y 120")
        if not math.isfinite(args.interval) or not 1 <= args.interval <= 300:
            raise ValueError("interval debe estar entre 1 y 300 segundos")
        if args.count * args.interval > 3600:
            raise ValueError("La ejecución debe caber en una hora nominal")
        policy = ContinuityPolicy(expected_interval_seconds=args.interval,
                            max_interval_seconds=max(45, args.interval * 1.5),
                            min_interval_seconds=max(1, args.interval * 0.5),
                            stale_after_seconds=args.interval * 3,
                            max_counter_rate=args.max_counter_rate)
    async with client_factory(settings) as client:
        if args.command == "inventory":
            return await inventory(client, args.limit), 0
        if args.command == "inspect":
            return await inspect_interface(client, args.interface_index), 0
        results = []
        with TelemetryStore(args.database) as store:
            for index in range(args.count):
                capture = await capture_once(settings, client)
                persisted = store.ingest(capture.attempt, capture.observations)
                if not results:
                    start = capture.attempt.started_at
                results.append({"poll_id": capture.attempt.poll_id,
                                "status": capture.attempt.status,
                                "error_code": capture.attempt.error_code,
                                "warnings": list(capture.warnings),
                                "persistence": asdict(persisted)})
                end = capture.attempt.finished_at + timedelta(microseconds=1)
                if index + 1 < args.count:
                    await asyncio.sleep(args.interval)
            as_of = max(datetime.now(UTC), end)
            history = {metric: store.history(
                settings.device_id, settings.interface_id, metric, start, end, policy, as_of
            ) for metric in ("if_in_octets", "if_out_octets", "if_oper_status", "if_speed")}
            output = {"polls": results, "reconciliation": store.reconcile(),
                      "history": history,
                      "coverage": "Una interfaz del router; no identifica clientes ni radio Wi-Fi"}
        return output, 0 if all(item["status"] == "success" for item in results) else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("inventory", help="Solo nombres e índices; recorrido acotado")
    listing.add_argument("--limit", type=int, default=16)
    inspect = commands.add_parser("inspect", help="Metadatos y soporte de OIDs de una interfaz")
    inspect.add_argument("--interface-index", required=True, type=int)
    poll = commands.add_parser("poll", help="Capturar y persistir una cantidad finita de polls")
    poll.add_argument("--count", type=int, default=2)
    poll.add_argument("--interval", type=float, default=30)
    poll.add_argument("--max-counter-rate", type=float)
    poll.add_argument("--database", default="output/telemetry-snmp.sqlite3")
    for command in (listing, inspect, poll):
        command.add_argument("--config", default="config/snmp.local.json")
    args = parser.parse_args(argv)
    try:
        settings = SnmpSettings.load(args.config)
        output, status = asyncio.run(run(settings, args))
    except SnmpFailure as error:
        print(json.dumps({"error_code": error.code}))
        return 1
    except (ValueError, OSError, sqlite3.Error) as error:
        # No se imprime ninguna excepción de biblioteca/red ni la configuración.
        if isinstance(error, ValueError):
            parser.exit(2, "Configuración: " + str(error) + "\n")
        parser.exit(2, "No se pudo leer la configuración o abrir/escribir la base local\n")
    print(json.dumps(output, ensure_ascii=False, indent=2, allow_nan=False))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
