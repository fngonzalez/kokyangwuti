"""Tests con cliente falso; no se consultan equipos ni se abren transports UDP."""

import argparse
from dataclasses import replace
from datetime import timedelta
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.telemetry.model import timestamp
from src.telemetry.rates import RatePolicy, calculate_rate
from src.telemetry.snmp import (
    COUNTERS, IF_DISCONTINUITY, IF_MAC, IF_NAME, IF_SPEED, IF_STATUS,
    SYS_DESCR, SYS_OBJECT_ID, SYS_UPTIME, PySnmpClient, SnmpFailure,
    SnmpResponse, SnmpSettings, SnmpValue, capture_once, inspect_interface, inventory,
)
from src.telemetry.snmp_cli import run
from src.telemetry.store import TelemetryStore

T0 = timestamp("2026-10-21T12:00:00Z")


def settings(**changes):
    values = dict(host="192.0.2.1", community="fixture-secret-not-a-real-credential",
                  interface_index=4, device_id="synthetic-router", interface_id="inventory-uplink",
                  interface_identity="incarnation-a", expected_sys_object_id="1.3.6.1.4.1.99999.1",
                  expected_interface_name="fixture-ether", expected_interface_mac="02:00:00:00:00:04")
    values.update(changes)
    return SnmpSettings(**values)


def response(seconds=1, bits=64, **changes):
    values = {
        SYS_DESCR: SnmpValue("octets", b"Synthetic router"),
        SYS_OBJECT_ID: SnmpValue("oid", "1.3.6.1.4.1.99999.1"),
        SYS_UPTIME: SnmpValue("ticks", 100000 + int(seconds * 100)),
        IF_NAME + ".4": SnmpValue("octets", b"fixture-ether"),
        IF_MAC + ".4": SnmpValue("octets", bytes.fromhex("020000000004")),
        IF_DISCONTINUITY + ".4": SnmpValue("ticks", 0),
        IF_SPEED + ".4": SnmpValue("gauge", 100000000),
        IF_STATUS + ".4": SnmpValue("integer", 1),
        COUNTERS[bits][0] + ".4": SnmpValue(f"counter{bits}", 2**bits - 1),
        COUNTERS[bits][1] + ".4": SnmpValue(f"counter{bits}", 2000),
    }
    values.update(changes)
    return SnmpResponse(values, T0 + timedelta(seconds=seconds))


class FakeClient:
    def __init__(self, result=None, failure=None):
        self.result = result or response()
        self.failure = failure
        self.requests = []

    async def get(self, oids):
        self.requests.append(tuple(oids))
        if self.failure:
            raise self.failure
        return self.result

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass


class SettingsTests(unittest.TestCase):
    def test_secret_not_in_repr_or_configuration_error(self):
        self.assertNotIn("fixture-secret", repr(settings()))
        with self.assertRaises(ValueError) as error:
            settings(version="fixture-secret-not-a-version")
        self.assertNotIn("fixture-secret", str(error.exception))

    def test_invalid_network_or_unbounded_options(self):
        for changes in ({"host": "some-host.example"}, {"retries": 3},
                        {"timeout_seconds": 100}, {"timeout_seconds": float("nan")},
                        {"port": 0}, {"community": ""}, {"counter_bits": 16}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                settings(**changes)

    def test_binding_required_without_guessing_identity(self):
        for name in ("interface_index", "expected_interface_mac", "expected_sys_object_id",
                     "interface_identity", "expected_interface_name"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                settings(**{name: None}).require_binding()

    def test_local_config_and_environment_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snmp.local.json"
            path.write_text(json.dumps({"host": "192.0.2.1", "community": "local-fixture"}), encoding="utf-8")
            with patch.dict(os.environ, {"KOKYANGWUTI_SNMP_COMMUNITY": "environment-fixture"}):
                config = SnmpSettings.load(path)
            self.assertEqual(config.community, "environment-fixture")

    def test_malformed_config_does_not_echo_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snmp.local.json"
            path.write_text('{"community":"fixture-secret", INVALID}', encoding="utf-8")
            with self.assertRaises(ValueError) as error:
                SnmpSettings.load(path)
            self.assertNotIn("fixture-secret", str(error.exception))


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def capture(self, client=None, config=None):
        clock = iter((T0, T0 + timedelta(seconds=2)))
        return await capture_once(config or settings(), client or FakeClient(),
                                  clock=lambda: next(clock), poll_id="fixture-poll")

    async def test_contract_times_types_and_only_selected_interface(self):
        client = FakeClient()
        result = await self.capture(client)
        self.assertEqual(result.attempt.status, "success")
        self.assertEqual(len(result.observations), 4)
        self.assertEqual(result.observations[0].raw_value, 18446744073709551615)
        for item in result.observations:
            self.assertIsNone(item.observed_at)
            self.assertEqual(item.queried_at, T0 + timedelta(seconds=1))
            self.assertEqual(item.ingested_at, T0 + timedelta(seconds=2))
            self.assertEqual(item.interface_identity, "incarnation-a")
        self.assertEqual(len(client.requests), 1)
        self.assertTrue(all(oid in (SYS_OBJECT_ID, SYS_UPTIME) or oid.endswith(".4")
                            for oid in client.requests[0]))

    async def test_timeout_persists_failed_attempt_without_fake_observations(self):
        result = await self.capture(FakeClient(failure=SnmpFailure("snmp_timeout")))
        self.assertEqual(result.attempt.error_code, "snmp_timeout")
        self.assertEqual(result.observations, ())
        with TelemetryStore(":memory:") as store:
            store.ingest(result.attempt, result.observations)
            self.assertEqual(store.reconcile()["polls_error"], 1)
            self.assertEqual(store.reconcile()["metric_slots_without_row"], 4)

    async def test_reused_index_with_different_binding_is_unknown(self):
        for oid, value in ((IF_NAME + ".4", SnmpValue("octets", b"different-interface")),
                           (IF_MAC + ".4", SnmpValue("octets", bytes.fromhex("020000000099"))),
                           (SYS_OBJECT_ID, SnmpValue("oid", "1.3.6.1.4.1.99999.2"))):
            with self.subTest(oid=oid):
                sample = response()
                sample.values[oid] = value
                result = await self.capture(FakeClient(sample))
                self.assertEqual(result.attempt.error_code, "interface_binding_mismatch")
                self.assertTrue(all(item.quality == "unknown" and item.raw_value is None
                                    for item in result.observations))

    async def test_missing_counter_is_partial_and_not_zero(self):
        sample = response()
        sample.values[COUNTERS[64][1] + ".4"] = SnmpValue("missing", None)
        result = await self.capture(FakeClient(sample))
        self.assertEqual(result.attempt.status, "partial")
        self.assertEqual(result.observations[1].quality, "missing")
        self.assertIsNone(result.observations[1].raw_value)
        with TelemetryStore(":memory:") as store:
            store.ingest(result.attempt, result.observations)
            self.assertEqual(store.reconcile()["metric_results_good"], 3)

    async def test_invalid_counter_type_and_range(self):
        for value in (SnmpValue("integer", 100), SnmpValue("counter64", -1), SnmpValue("counter64", 2**64)):
            with self.subTest(value=value):
                sample = response()
                sample.values[COUNTERS[64][0] + ".4"] = value
                result = await self.capture(FakeClient(sample))
                self.assertEqual(result.observations[0].quality, "error")

    async def test_missing_continuity_preserves_raw_but_prevents_rate(self):
        first = (await self.capture()).observations[0]
        sample = response()
        sample.values[IF_DISCONTINUITY + ".4"] = SnmpValue("missing", None)
        second = (await self.capture(FakeClient(sample)))
        self.assertIn("discontinuity_unavailable", second.warnings)
        current = replace(second.observations[0], queried_at=first.queried_at + timedelta(seconds=30),
                          ingested_at=first.ingested_at + timedelta(seconds=30))
        rate = calculate_rate(first, current, RatePolicy(max_counter_rate=12500000))
        self.assertEqual(rate.reason, "discontinuity_unknown")
        self.assertIsNone(rate.value)

    async def test_32_bit_mode_is_explicit(self):
        result = await self.capture(FakeClient(response(bits=32)), settings(counter_bits=32))
        self.assertEqual(result.observations[0].metric.counter_bits, 32)
        self.assertEqual(result.observations[0].raw_value, 2**32 - 1)

    async def test_collector_clock_regression_is_recorded(self):
        times = iter((T0, T0 - timedelta(seconds=1)))
        result = await capture_once(settings(), FakeClient(), clock=lambda: next(times))
        self.assertEqual(result.attempt.error_code, "collector_clock_regressed")
        self.assertEqual(result.observations, ())

    async def test_inventory_stops_at_subtree_boundary(self):
        client = FakeClient()
        client.next = AsyncMock(side_effect=[
            SnmpResponse({IF_NAME + ".4": SnmpValue("octets", b"fixture-ether")}, T0),
            SnmpResponse({"1.3.6.1.2.1.31.1.1.1.2.1": SnmpValue("integer", 0)}, T0),
        ])
        result = await inventory(client)
        self.assertEqual(result["interfaces"], [{"interface_index": 4, "interface_name": "fixture-ether"}])
        self.assertEqual(client.next.await_count, 2)

    async def test_inventory_rejects_non_advancing_oid(self):
        client = FakeClient()
        client.next = AsyncMock(return_value=SnmpResponse({IF_NAME + ".4": SnmpValue("octets", b"x")}, T0))
        with self.assertRaises(SnmpFailure):
            await inventory(client)

    async def test_inspect_reports_binding_and_counter_support(self):
        result = await inspect_interface(FakeClient(), 4)
        self.assertEqual(result["counter_bits_supported"], [64])
        self.assertEqual(result["expected_interface_mac"], "02:00:00:00:00:04")
        self.assertTrue(result["discontinuity_supported"])

    async def test_two_polls_persist_and_produce_history(self):
        client = FakeClient()
        now = timestamp("2026-10-21T12:00:00Z")
        times = iter(now + timedelta(seconds=i) for i in (0, 2, 3, 5))
        samples = iter((response(1), response(4)))
        client.get = AsyncMock(side_effect=lambda _: next(samples))
        with tempfile.TemporaryDirectory() as directory:
            args = argparse.Namespace(command="poll", count=2, interval=30,
                                      max_counter_rate=12500000,
                                      database=str(Path(directory) / "telemetry.sqlite3"))
            # Usar capture real con reloj determinista; solo se simula la espera y el transporte.
            async def deterministic_capture(config, transport):
                return await capture_once(config, transport, clock=lambda: next(times))
            with patch("src.telemetry.snmp_cli.capture_once", side_effect=deterministic_capture), \
                 patch("src.telemetry.snmp_cli.asyncio.sleep", new_callable=AsyncMock):
                result, status = await run(settings(), args, lambda _: client)
            self.assertEqual(status, 0)
            self.assertEqual(result["reconciliation"]["polls"], 2)
            self.assertEqual(result["reconciliation"]["observation_rows"], 8)
            self.assertEqual(len({item["poll_id"] for item in result["polls"]}), 2)

    async def test_invalid_run_limit_does_not_open_client(self):
        args = argparse.Namespace(command="poll", count=0, interval=30, max_counter_rate=None)
        def forbidden(_):
            raise AssertionError("No debe abrir cliente")
        with self.assertRaises(ValueError):
            await run(settings(), args, forbidden)


@unittest.skipUnless(importlib.util.find_spec("pysnmp"), "Dependencia SNMP opcional no instalada")
class AsnTypeTests(unittest.TestCase):
    def test_real_asn_types_preserve_unsigned_64_and_missing_values(self):
        from pysnmp.proto import rfc1902, rfc1905
        self.assertEqual(PySnmpClient.convert(rfc1902.Counter64(2**64 - 1)), SnmpValue("counter64", 2**64 - 1))
        self.assertEqual(PySnmpClient.convert(rfc1902.TimeTicks(123)), SnmpValue("ticks", 123))
        self.assertEqual(PySnmpClient.convert(rfc1902.Gauge32(100)), SnmpValue("gauge", 100))
        self.assertEqual(PySnmpClient.convert(rfc1902.Integer32(1)), SnmpValue("integer", 1))
        self.assertEqual(PySnmpClient.convert(rfc1902.OctetString(b"abc")), SnmpValue("octets", b"abc"))
        self.assertEqual(PySnmpClient.convert(rfc1905.NoSuchInstance()), SnmpValue("missing", None))


@unittest.skipUnless(importlib.util.find_spec("pysnmp"), "Dependencia SNMP opcional no instalada")
class TransportErrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_library_exception_is_sanitized(self):
        config = settings()
        client = PySnmpClient(config)
        client.engine, client.auth, client.target = object(), object(), object()
        client.api = SimpleNamespace(
            get_cmd=AsyncMock(side_effect=ValueError(config.community)),
            ContextData=lambda: object(), ObjectType=lambda value: value,
            ObjectIdentity=lambda value: value,
        )
        with self.assertRaises(SnmpFailure) as error:
            await client.get((SYS_UPTIME,))
        self.assertEqual(error.exception.code, "snmp_transport_error")
        self.assertNotIn(config.community, str(error.exception))

    async def test_auth_initialization_error_is_sanitized_and_engine_closed(self):
        config = settings()
        engine = MagicMock()
        with patch("pysnmp.hlapi.v3arch.asyncio.SnmpEngine", return_value=engine), \
             patch("pysnmp.hlapi.v3arch.asyncio.CommunityData", side_effect=ValueError(config.community)):
            with self.assertRaises(SnmpFailure) as error:
                await PySnmpClient(config).__aenter__()
        self.assertEqual(error.exception.code, "snmp_transport_error")
        self.assertNotIn(config.community, str(error.exception))
        engine.close_dispatcher.assert_called_once()


if __name__ == "__main__":
    unittest.main()
