"""Casos offline de contrato, persistencia e histórico. No se importa el prototipo CGI."""

from contextlib import closing, redirect_stdout
from dataclasses import replace
from datetime import timedelta
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from src.telemetry import (
    ConflictError, Metric, Observation, PollAttempt, RatePolicy,
    TelemetryStore, calculate_rate,
)
from src.telemetry.__main__ import main
from src.telemetry.model import timestamp

T0 = timestamp("2026-10-21T12:00:00Z")
RX = Metric("if_in_octets", "counter", "octets", 64)
TX = Metric("if_out_octets", "counter", "octets", 64)
POLICY = RatePolicy(max_counter_rate=10000)


def observation(seconds=0, value=1000, **changes):
    queried = T0 + timedelta(seconds=seconds)
    fields = dict(
        poll_id=f"poll-{seconds}", device_id="synthetic-router",
        interface_id="inventory-uplink", interface_identity="incarnation-a",
        metric=RX, raw_value=value, queried_at=queried,
        ingested_at=queried + timedelta(seconds=1), uptime_ticks=100000 + int(seconds * 100),
        discontinuity_ticks=0, interface_index=4, interface_name="synthetic-ether",
    )
    fields.update(changes)
    return Observation(**fields)


def attempt(item, metrics=None, status="success", error_code=None):
    return PollAttempt(
        item.poll_id, item.device_id, item.interface_id, item.interface_identity,
        item.queried_at - timedelta(milliseconds=100), item.queried_at,
        tuple(metrics or [item.metric]), status, error_code,
    )


class OfflineTest(unittest.TestCase):
    def setUp(self):
        # Si el núcleo intenta abrir una conexión de red, el test falla.
        guard = patch("socket.socket", side_effect=AssertionError("Red prohibida en iteración 1"))
        guard.start()
        self.addCleanup(guard.stop)


class ContractTests(OfflineTest):
    def test_round_trip_preserves_three_times_and_integer(self):
        item = observation(value=2**64 - 1, observed_at=T0 - timedelta(seconds=2))
        restored = Observation.from_dict(json.loads(json.dumps(item.to_dict())))
        self.assertEqual(item, restored)
        self.assertEqual(restored.raw_value, 18446744073709551615)
        self.assertNotEqual(restored.observed_at, restored.queried_at)
        self.assertNotEqual(restored.queried_at, restored.ingested_at)

    def test_rejects_invalid_contracts(self):
        for changes in (
            {"raw_value": 2**64}, {"raw_value": -1}, {"raw_value": 1.0},
            {"raw_value": True}, {"quality": "missing"},
            {"queried_at": T0.replace(tzinfo=None)}, {"schema_version": 2},
            {"interface_identity": ""}, {"ingested_at": T0 - timedelta(seconds=1)},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                observation(**changes)

    def test_missing_sample_is_not_zero(self):
        item = observation(value=None, quality="missing")
        self.assertIsNone(item.raw_value)
        self.assertEqual(calculate_rate(None, item, POLICY).reason, "current_sample_missing")

    def test_ingestion_time_not_in_dedup_identity(self):
        original = observation()
        replay = replace(original, ingested_at=T0 + timedelta(days=1))
        self.assertEqual(original.dedup_key, replay.dedup_key)
        self.assertEqual(original.content, replay.content)


class RateTests(OfflineTest):
    def test_actual_elapsed_time_and_unit_conversion(self):
        result = calculate_rate(observation(value=100), observation(31.5, 415), POLICY)
        self.assertEqual(result.reason, "ok")
        self.assertEqual(result.value, 10)
        self.assertEqual(result.bits_per_second, 80)
        self.assertEqual(result.elapsed_seconds, 31.5)

    def test_wrap_32_and_64_bits(self):
        for bits in (32, 64):
            with self.subTest(bits=bits):
                metric = Metric("rx", "counter", "octets", bits)
                before = observation(value=2**bits - 100, metric=metric)
                after = observation(30, 200, metric=metric)
                result = calculate_rate(before, after, POLICY)
                self.assertEqual(result.delta, 300)
                self.assertEqual(result.value, 10)
                self.assertTrue(result.wrapped)

    def test_reboot_even_when_counter_increased(self):
        result = calculate_rate(observation(), observation(30, 2000, uptime_ticks=100), POLICY)
        self.assertIsNone(result.value)
        self.assertEqual(result.reason, "uptime_regressed")

    def test_counter_discontinuity(self):
        result = calculate_rate(observation(), observation(30, 2000, discontinuity_ticks=101000), POLICY)
        self.assertEqual(result.reason, "counter_discontinuity")

    def test_interface_identity_change_even_when_index_is_reused(self):
        result = calculate_rate(observation(), observation(30, 2000, interface_identity="incarnation-b"), POLICY)
        self.assertEqual(result.reason, "interface_identity_changed")
        self.assertIsNone(result.value)

    def test_index_and_name_change_do_not_change_inventory_identity(self):
        result = calculate_rate(observation(), observation(30, 1300, interface_index=99, interface_name="renamed"), POLICY)
        self.assertEqual(result.value, 10)

    def test_absence_and_invalid_intervals(self):
        before = observation()
        cases = [
            (None, observation(30), "no_previous_sample"),
            (before, observation(90), "interval_too_long"),
            (before, observation(0), "non_positive_interval"),
            (before, observation(-1), "non_positive_interval"),
            (before, observation(0.5), "interval_too_short"),
            (observation(value=None, quality="error"), observation(30), "previous_sample_error"),
        ]
        for previous, current, reason in cases:
            with self.subTest(reason=reason):
                result = calculate_rate(previous, current, POLICY)
                self.assertIsNone(result.value)
                self.assertEqual(result.reason, reason)

    def test_unknown_continuity_or_bound_does_not_guess(self):
        for changes, policy, reason in (
            ({"uptime_ticks": None}, POLICY, "uptime_missing"),
            ({"discontinuity_ticks": None}, POLICY, "discontinuity_unknown"),
            ({}, RatePolicy(), "counter_rate_bound_missing"),
        ):
            with self.subTest(reason=reason):
                result = calculate_rate(observation(), observation(30, 1300, **changes), policy)
                self.assertIsNone(result.value)
                self.assertEqual(result.reason, reason)

    def test_ambiguous_multiple_wraps_even_with_positive_delta(self):
        metric = Metric("rx", "counter", "octets", 32)
        result = calculate_rate(observation(metric=metric), observation(30, 1300, metric=metric),
                                RatePolicy(max_counter_rate=2**32))
        self.assertEqual(result.reason, "multiple_wraps_possible")

    def test_impossible_delta_rejected(self):
        result = calculate_rate(observation(value=100), observation(30, 400000), POLICY)
        self.assertEqual(result.reason, "counter_delta_exceeds_bound")

    def test_definition_change_and_gauge(self):
        result = calculate_rate(observation(), observation(30, metric=Metric(RX.name, "counter", "octets", 32)), POLICY)
        self.assertEqual(result.reason, "counter_definition_changed")
        gauge = observation(metric=Metric("cpu", "gauge", "percent"), value=20)
        self.assertEqual(calculate_rate(None, gauge, POLICY).reason, "not_a_counter")

    def test_invalid_policies(self):
        for changes in ({"max_counter_rate": float("nan")}, {"max_counter_rate": 0},
                        {"max_interval_seconds": 20}, {"min_interval_seconds": -1}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                RatePolicy(**changes)


class StoreTests(OfflineTest):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "telemetry.sqlite3"
        self.store = TelemetryStore(self.path)
        self.addCleanup(self.store.close)

    def add(self, item):
        return self.store.ingest(attempt(item), [item])

    def history(self, start=0, end=180, as_of=180, **changes):
        fields = dict(device_id="synthetic-router", interface_id="inventory-uplink",
                      metric=RX.name, start=T0 + timedelta(seconds=start),
                      end=T0 + timedelta(seconds=end), policy=POLICY,
                      as_of=T0 + timedelta(seconds=as_of))
        fields.update(changes)
        return self.store.history(**fields)

    def test_idempotence_preserves_first_ingestion_time(self):
        item = observation()
        self.assertEqual(self.add(item).inserted_observations, 1)
        result = self.add(replace(item, ingested_at=T0 + timedelta(days=1)))
        self.assertEqual(result.duplicate_observations, 1)
        self.assertEqual(result.inserted_observations, 0)
        self.assertEqual(self.store.reconcile()["polls"], 1)
        self.assertEqual(self.store.reconcile()["observation_rows"], 1)
        self.assertEqual(self.history(end=1, as_of=1)[0]["ingested_at"], item.to_dict()["ingested_at"])

    def test_conflicting_replay_cannot_overwrite(self):
        item = observation()
        self.add(item)
        with self.assertRaises(ConflictError):
            self.add(replace(item, raw_value=2000))
        self.assertEqual(self.history(end=1, as_of=1)[0]["raw_value"], 1000)

    def test_atomicity_when_sqlite_insert_fails(self):
        self.store.connection.execute("""
            CREATE TRIGGER synthetic_failure BEFORE INSERT ON observations
            WHEN NEW.metric='if_out_octets' BEGIN SELECT RAISE(ABORT,'synthetic disk failure'); END
        """)
        rx, tx = observation(), observation(metric=TX)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.ingest(attempt(rx, [RX, TX]), [rx, tx])
        self.assertEqual(self.store.reconcile()["polls"], 0)
        self.assertEqual(self.store.reconcile()["metric_slots_expected"], 0)
        self.assertEqual(self.store.reconcile()["observation_rows"], 0)
        self.store.connection.execute("DROP TRIGGER synthetic_failure")
        self.assertEqual(self.add(rx).inserted_observations, 1)

    def test_invalid_batch_does_not_persist(self):
        item = observation()
        with self.assertRaises(ValueError):
            self.store.ingest(attempt(item, [RX, TX]), [item])
        self.assertEqual(self.store.reconcile()["polls"], 0)

    def test_partial_poll_error_and_reconciliation_units(self):
        item = observation()
        self.store.ingest(attempt(item, [RX, TX], "partial", "metric_timeout"), [item])
        failed = observation(30)
        self.store.ingest(attempt(failed, [RX, TX], "error", "timeout"), [])
        counts = self.store.reconcile()
        self.assertEqual(counts, dict(
            polls=2, polls_success=0, polls_partial=1, polls_error=1,
            metric_slots_expected=4, observation_rows=1, metric_results_good=1,
            metric_results_not_good=0, metric_slots_without_row=3,
            metric_slots_without_good_result=3,
        ))

    def test_failed_poll_breaks_rate_and_marks_missing_observation(self):
        self.add(observation())
        failed = observation(30)
        self.store.ingest(attempt(failed, status="error", error_code="timeout"), [])
        self.add(observation(60, 1600))
        rows = self.history(end=61, as_of=61)
        self.assertEqual(rows[1]["kind"], "missing")
        self.assertEqual(rows[1]["error_code"], "timeout")
        self.assertIsNone(rows[1]["raw_value"])
        self.assertEqual(rows[2]["rate"]["reason"], "previous_poll_missing")

    def test_explicit_missing_result_has_quality_and_no_rate(self):
        missing = observation(value=None, quality="missing")
        self.store.ingest(attempt(missing, status="error", error_code="no_value"), [missing])
        rows = self.history(end=1, as_of=1)
        self.assertEqual(rows[0]["quality"], "missing")
        self.assertEqual(rows[0]["rate"]["reason"], "current_sample_missing")
        self.assertEqual(self.store.reconcile()["metric_results_not_good"], 1)

    def test_unrecorded_polls_produce_explicit_gap(self):
        self.add(observation())
        self.add(observation(120, 2200))
        rows = self.history(end=121, as_of=121)
        self.assertEqual([row["kind"] for row in rows], ["observation", "gap", "observation"])
        self.assertEqual(rows[1]["start"], (T0 + timedelta(seconds=30)).isoformat())
        self.assertEqual(rows[1]["end"], (T0 + timedelta(seconds=120)).isoformat())
        self.assertEqual(rows[2]["rate"]["reason"], "interval_too_long")

    def test_history_boundaries_and_predecessor_context(self):
        for seconds, value in ((0, 1000), (30, 1300), (60, 1600)):
            self.add(observation(seconds, value))
        self.add(observation(30, 99, device_id="other-device", poll_id="other-device-poll"))
        self.add(observation(30, 88, interface_id="other-interface", poll_id="other-interface-poll"))
        rows = self.history(start=30, end=60, as_of=60)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["raw_value"], 1300)
        self.assertEqual(rows[0]["rate"]["value"], 10)

    def test_identity_change_visible_in_history(self):
        self.add(observation())
        self.add(observation(30, 1300, interface_identity="incarnation-b"))
        self.assertEqual(self.history(end=31, as_of=31)[1]["rate"]["reason"], "interface_identity_changed")

    def test_out_of_order_ingestion_uses_query_time(self):
        self.add(observation(30, 1300))
        self.add(observation())
        self.assertEqual(self.history(end=31, as_of=31)[1]["rate"]["value"], 10)

    def test_staleness_is_explicit_without_rewriting_historical_quality(self):
        self.add(observation())
        rows = self.history(end=1, as_of=200)
        self.assertEqual(rows[0]["quality"], "good")
        self.assertEqual(rows[0]["age_seconds"], 200)
        self.assertEqual(rows[0]["freshness"], "stale")
        self.assertEqual(rows[0]["current_state"], "unknown")

    def test_empty_leading_and_trailing_gaps(self):
        empty = self.history()
        self.assertEqual(len(empty), 1)
        self.assertEqual(empty[0]["kind"], "gap")
        self.add(observation(90))
        rows = self.history()
        self.assertEqual([row["kind"] for row in rows], ["gap", "observation", "gap"])

    def test_unsigned_64_counter_survives_sqlite_and_reopen(self):
        self.add(observation(value=2**64 - 1))
        with TelemetryStore(self.path) as reopened:
            rows = reopened.history("synthetic-router", "inventory-uplink", RX.name,
                                    T0, T0 + timedelta(seconds=1), POLICY,
                                    T0 + timedelta(seconds=1))
        self.assertEqual(rows[0]["raw_value"], 18446744073709551615)

    def test_rejects_foreign_or_newer_database_without_migration(self):
        for name, version in (("foreign", 0), ("newer", 2), ("foreign-v1", 1)):
            path = Path(self.temp.name) / (name + ".sqlite3")
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("CREATE TABLE unrelated (value INTEGER)")
                connection.execute(f"PRAGMA user_version={version}")
            with self.subTest(name=name), self.assertRaises(ValueError):
                TelemetryStore(path)
            with closing(sqlite3.connect(path)) as connection:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], version)
                self.assertEqual(connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [("unrelated",)])

    def test_history_rejects_invalid_or_future_period(self):
        for start, end, as_of in ((30, 0, 60), (0, 0, 60), (0, 60, 30)):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                self.history(start, end, as_of)

    def test_cli_fixture_replay_and_historical_query(self):
        fixture = Path(__file__).parent / "fixtures" / "session.json"
        arguments = ["ingest", str(fixture), "--database", str(self.path)]
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(arguments), 0)
        first = json.loads(output.getvalue())
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(arguments), 0)
        second = json.loads(output.getvalue())
        self.assertEqual(first["reconciliation"], second["reconciliation"])
        self.assertEqual(first["reconciliation"]["polls"], 5)
        self.assertTrue(all(row["inserted_observations"] == 0 for row in second["ingestion"]))
        with redirect_stdout(io.StringIO()) as output:
            main(["history", "--database", str(self.path), "--device", "synthetic-router",
                  "--interface", "inventory-uplink", "--metric", RX.name,
                  "--start", "2026-10-21T12:00:00Z", "--end", "2026-10-21T12:03:01Z",
                  "--as-of", "2026-10-21T12:03:01Z", "--max-counter-rate", "10000"])
        rows = json.loads(output.getvalue())
        self.assertTrue(any(row["kind"] == "missing" for row in rows))
        self.assertTrue(any(row["kind"] == "gap" for row in rows))
        self.assertTrue(any(row["rate"] and row["rate"]["reason"] == "uptime_regressed" for row in rows))


if __name__ == "__main__":
    unittest.main()
