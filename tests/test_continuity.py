"""Política e integración sintéticas. Estos tests prohíben abrir sockets."""

from contextlib import redirect_stdout
from dataclasses import asdict, replace
from datetime import timedelta
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from src.telemetry import (ContinuityPolicy, Metric, Observation, PollAttempt,
                           TelemetryStore, calculate_traffic_rate)
from src.telemetry.__main__ import main
from src.telemetry.model import timestamp

T0 = timestamp("2026-10-21T12:00:00Z")
RX = Metric("if_in_octets", "counter", "octets", 64)
POLICY = ContinuityPolicy()


def sample(seconds=0, value=1000, **changes):
    queried = T0 + timedelta(seconds=seconds)
    values = dict(poll_id=f"synthetic-{seconds}", device_id="synthetic-device",
                  interface_id="inventory-port", interface_identity="incarnation-a",
                  metric=RX, raw_value=value, queried_at=queried,
                  ingested_at=queried + timedelta(seconds=1), uptime_ticks=100000 + round(seconds * 100),
                  discontinuity_ticks=None, interface_index=1, interface_name="synthetic-port")
    values.update(changes)
    return Observation(**values)


def poll(item, status="success", error_code=None):
    return PollAttempt(item.poll_id, item.device_id, item.interface_id, item.interface_identity,
                       item.queried_at, item.queried_at, (item.metric,), status, error_code)


class NoNetwork(unittest.TestCase):
    def setUp(self):
        guard = patch("socket.socket", side_effect=AssertionError("Esta iteración no admite red"))
        guard.start()
        self.addCleanup(guard.stop)


class PureRateTests(NoNetwork):
    def test_normal_inference_in_bits_per_second_using_actual_interval(self):
        previous, current = sample(value=100), sample(31.5, 415)
        result = calculate_traffic_rate(previous, current, POLICY)
        self.assertEqual((result.value_bps, result.state, result.unit), (80, "inferida", "bits/s"))
        self.assertEqual(result.elapsed_seconds, 31.5)
        self.assertEqual(result.time_basis, "queried_at")
        self.assertEqual(result.previous_sample["observation_id"], previous.dedup_key)
        self.assertEqual(result.current_sample["observation_id"], current.dedup_key)
        self.assertEqual(result.previous_sample["raw_value"], 100)
        self.assertEqual(result.current_sample["raw_value"], 415)
        self.assertEqual(result.policy, POLICY.snapshot())
        self.assertEqual(result.schema_version, 2)

    def test_device_timestamp_is_not_mixed_into_query_clock(self):
        previous = sample(observed_at=T0 - timedelta(days=1))
        current = sample(30, 1300, observed_at=T0 + timedelta(days=1))
        self.assertEqual(calculate_traffic_rate(previous, current, POLICY).value_bps, 80)

    def test_zero_is_a_valid_inferred_rate_not_null(self):
        result = calculate_traffic_rate(sample(), sample(30), POLICY)
        self.assertEqual(result.state, "inferida")
        self.assertEqual(result.value_bps, 0)
        missing = calculate_traffic_rate(sample(), sample(30, None, quality="missing"), POLICY)
        self.assertEqual(missing.state, "no_calculable")
        self.assertIsNone(missing.value_bps)

    def test_decreasing_counter_without_mark_is_never_treated_as_wrap(self):
        for start, end in ((1000, 50), (2**64 - 100, 200)):
            with self.subTest(start=start):
                result = calculate_traffic_rate(sample(value=start), sample(30, end), POLICY)
                self.assertEqual((result.state, result.reason), ("no_calculable", "contador_decreciente"))
                self.assertIsNone(result.value_bps)

    def test_agent_restart_even_if_counter_did_not_decrease(self):
        result = calculate_traffic_rate(sample(), sample(30, 2000, uptime_ticks=100), POLICY)
        self.assertEqual(result.reason, "reinicio_agente")
        self.assertIsNone(result.value_bps)

    def test_uptime_wrap_is_detected_as_ambiguous_and_next_pair_can_resume(self):
        previous = sample(uptime_ticks=2**32 - 1500)
        wrapped = sample(30, 1300, uptime_ticks=1500)
        result = calculate_traffic_rate(previous, wrapped, POLICY)
        self.assertEqual((result.state, result.reason), ("no_calculable", "wrap_uptime_ambiguo"))
        recovered = calculate_traffic_rate(wrapped, sample(60, 1600, uptime_ticks=4500), POLICY)
        self.assertEqual((recovered.state, recovered.value_bps), ("inferida", 80))

    def test_wrap_tolerance_only_classifies_and_never_accepts_the_ambiguous_pair(self):
        previous = sample(uptime_ticks=2**32 - 1500)
        current = sample(30, 1300, uptime_ticks=1600)
        for policy, reason in ((POLICY, "wrap_uptime_ambiguo"),
                               (replace(POLICY, uptime_wrap_tolerance_seconds=0), "reinicio_agente")):
            with self.subTest(reason=reason):
                result = calculate_traffic_rate(previous, current, policy)
                self.assertIsNone(result.value_bps)
                self.assertEqual(result.reason, reason)

    def test_all_identity_components_prevent_cross_entity_rates(self):
        for changes in ({"device_id": "different"}, {"interface_id": "different"},
                        {"interface_identity": "different"}):
            with self.subTest(changes=changes):
                result = calculate_traffic_rate(sample(), sample(30, 1300, **changes), POLICY)
                self.assertEqual(result.reason, "identidad_cambiada")

    def test_interval_short_long_zero_negative_and_configurable_window(self):
        for seconds in (10, 60, 0, -30):
            with self.subTest(seconds=seconds):
                result = calculate_traffic_rate(sample(), sample(seconds, 1300), POLICY)
                self.assertEqual(result.reason, "intervalo_anormal")
                self.assertEqual(result.elapsed_seconds, seconds)
        policy = replace(POLICY, max_interval_seconds=60)
        self.assertEqual(calculate_traffic_rate(sample(), sample(60, 1600), policy).value_bps, 80)

    def test_interval_boundaries_are_inclusive(self):
        for seconds, value in ((15, 1150), (45, 1450)):
            with self.subTest(seconds=seconds):
                self.assertEqual(calculate_traffic_rate(sample(), sample(seconds, value), POLICY).value_bps, 80)

    def test_missing_bad_quality_and_absent_uptime(self):
        cases = [
            (sample(30, None, quality="missing"), "muestra_ausente"),
            (sample(30, None, quality="error"), "calidad_no_aceptable"),
            (sample(30, None, quality="unknown"), "calidad_no_aceptable"),
            (sample(30, 1300, uptime_ticks=None), "uptime_ausente"),
        ]
        for current, reason in cases:
            with self.subTest(reason=reason):
                self.assertEqual(calculate_traffic_rate(sample(), current, POLICY).reason, reason)
        self.assertEqual(calculate_traffic_rate(None, sample(), POLICY).reason, "sin_muestra_anterior")

    def test_confirmation_requires_two_comparable_unchanged_marks(self):
        previous = sample(discontinuity_ticks=0)
        confirmed = calculate_traffic_rate(previous, sample(30, 1300, discontinuity_ticks=0), POLICY)
        self.assertEqual((confirmed.state, confirmed.reason, confirmed.value_bps),
                         ("calculada", "marca_sin_cambios", 80))
        self.assertEqual(calculate_traffic_rate(sample(), sample(30, 1300, discontinuity_ticks=0), POLICY).state, "inferida")
        changed = calculate_traffic_rate(previous, sample(30, 1300, discontinuity_ticks=100100), POLICY)
        self.assertEqual((changed.state, changed.reason), ("no_calculable", "discontinuidad_contador"))

    def test_marked_counter_wrap_requires_bound_and_preserves_32_64_precision(self):
        for bits in (32, 64):
            with self.subTest(bits=bits):
                metric = Metric("rx", "counter", "octets", bits)
                before = sample(value=2**bits - 100, metric=metric, discontinuity_ticks=0)
                after = sample(30, 200, metric=metric, discontinuity_ticks=0)
                self.assertEqual(calculate_traffic_rate(before, after, POLICY).reason, "cota_requerida_para_wrap")
                rate = calculate_traffic_rate(before, after, replace(POLICY, max_counter_rate=10000))
                self.assertEqual((rate.state, rate.value_bps, rate.wrapped), ("calculada", 80, True))

    def test_unmarked_32_bit_counter_is_not_inferred(self):
        metric = Metric("rx", "counter", "octets", 32)
        rate = calculate_traffic_rate(sample(metric=metric), sample(30, 1300, metric=metric), POLICY)
        self.assertEqual(rate.reason, "inferencia_requiere_64_bits")

    def test_optional_capacity_rejects_implausible_delta_and_multiple_wraps(self):
        rate = calculate_traffic_rate(sample(), sample(30, 2000), replace(POLICY, max_counter_rate=1))
        self.assertEqual(rate.reason, "delta_excede_cota")
        rate = calculate_traffic_rate(sample(), sample(30, 1300), replace(POLICY, max_counter_rate=2**64))
        self.assertEqual(rate.reason, "multiples_wraps_posibles")

    def test_hidden_reset_with_no_contradicting_signal_remains_only_inferred(self):
        # Mismos valores observables si el contador se reseteó y luego superó el anterior.
        rate = calculate_traffic_rate(sample(), sample(30, 1300), POLICY)
        self.assertEqual(rate.state, "inferida")
        self.assertNotEqual(rate.state, "calculada")

    def test_policy_fingerprint_changes_with_threshold_and_inputs_are_unchanged(self):
        before, after = sample(), sample(30, 1300)
        original = (before.to_dict(), after.to_dict(), POLICY.snapshot())
        rate = calculate_traffic_rate(before, after, POLICY)
        self.assertEqual(original, (before.to_dict(), after.to_dict(), POLICY.snapshot()))
        self.assertNotEqual(rate.policy_id, replace(POLICY, stale_after_seconds=120).policy_id)
        rate.previous_sample["raw_value"] = -1
        rate.policy["stale_after_seconds"] = -1
        self.assertEqual(before.raw_value, 1000)
        self.assertEqual(POLICY.stale_after_seconds, 90)

    def test_invalid_policy(self):
        for changes in ({"min_interval_seconds": 0}, {"max_interval_seconds": 20},
                        {"stale_after_seconds": float("nan")}, {"uptime_wrap_tolerance_seconds": -1}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(POLICY, **changes)


class HistoryAndLatestTests(NoNetwork):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "synthetic.sqlite3"
        self.store = TelemetryStore(self.path)
        self.addCleanup(self.store.close)

    def add(self, item):
        status = "success" if item.quality == "good" else "error"
        self.store.ingest(poll(item, status, None if status == "success" else "synthetic_missing"), [item])

    def history(self, start=0, end=400, as_of=400, **changes):
        fields = dict(device_id="synthetic-device", interface_id="inventory-port", metric=RX.name,
                      start=T0 + timedelta(seconds=start), end=T0 + timedelta(seconds=end),
                      as_of=T0 + timedelta(seconds=as_of), policy=POLICY)
        fields.update(changes)
        return self.store.history(**fields)

    def latest(self, seconds, policy=POLICY):
        return self.store.latest("synthetic-device", "inventory-port", RX.name, policy,
                                 T0 + timedelta(seconds=seconds))

    def test_missing_poll_in_middle_has_provenance_and_breaks_rate(self):
        self.add(sample())
        absent = sample(30)
        self.store.ingest(poll(absent, "error", "synthetic_timeout"), [])
        self.add(sample(60, 4000))
        self.add(sample(90, 4300))
        rows = self.history(end=91, as_of=91)
        self.assertEqual(rows[1]["kind"], "missing")
        self.assertEqual(rows[1]["rate"]["reason"], "muestra_ausente")
        self.assertEqual(rows[2]["rate"]["reason"], "muestra_ausente")
        self.assertEqual(rows[2]["rate"]["previous_sample"]["poll_id"], absent.poll_id)
        self.assertEqual(rows[2]["rate"]["previous_sample"]["kind"], "missing")
        self.assertIsNone(rows[2]["rate"]["value_bps"])
        self.assertEqual(rows[3]["rate"]["value_bps"], 80)

    def test_explicit_missing_observation_is_not_skipped(self):
        for item in (sample(), sample(30, None, quality="missing"), sample(60, 3000)):
            self.add(item)
        rows = self.history(end=61, as_of=61)
        self.assertEqual(rows[2]["rate"]["reason"], "muestra_ausente")
        self.assertIsNone(rows[2]["rate"]["previous_sample"]["raw_value"])

    def test_mixed_sequence_keeps_state_reason_and_gaps(self):
        for item in (sample(), sample(30, 1300), sample(60, 1600, discontinuity_ticks=0),
                     sample(90, 1900, discontinuity_ticks=0),
                     sample(120, 2200, interface_identity="incarnation-b"),
                     sample(150, 2100, interface_identity="incarnation-b"),
                     sample(300, 2400, interface_identity="incarnation-b")):
            self.add(item)
        rows = self.history(end=301, as_of=301)
        points = [row for row in rows if row["kind"] == "observation"]
        self.assertEqual([row["rate"]["state"] for row in points],
                         ["no_calculable", "inferida", "inferida", "calculada", "no_calculable", "no_calculable", "no_calculable"])
        self.assertEqual(points[4]["rate"]["reason"], "identidad_cambiada")
        self.assertEqual(points[5]["rate"]["reason"], "contador_decreciente")
        self.assertEqual(points[6]["rate"]["reason"], "intervalo_anormal")
        gaps = [row for row in rows if row["kind"] == "gap"]
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["rate"]["reason"], "muestra_ausente")

    def test_window_uses_predecessor_without_emitting_it_and_filters_entities(self):
        self.add(sample())
        self.add(sample(30, 1300))
        self.add(sample(30, 99999, device_id="other", poll_id="other-device"))
        self.add(sample(30, 99999, interface_id="other", poll_id="other-interface"))
        rows = self.history(start=30, end=31, as_of=31)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["rate"]["value_bps"], 80)
        self.assertEqual(rows[0]["rate"]["previous_sample"]["poll_id"], "synthetic-0")

    def test_latest_stale_has_age_unknown_state_and_no_current_rate(self):
        self.add(sample())
        self.add(sample(30, 1300))
        latest = self.latest(121)
        self.assertEqual(latest["age_seconds"], 91)
        self.assertEqual(latest["current_state"], "unknown")
        self.assertEqual(latest["rate"]["reason"], "dato_antiguo")
        self.assertIsNone(latest["rate"]["value_bps"])
        historical = self.history(end=31, as_of=121)
        self.assertEqual(historical[1]["rate"]["value_bps"], 80)
        self.assertEqual(historical[1]["rate"]["state"], "inferida")

    def test_latest_stale_threshold_is_configurable_and_boundary_is_inclusive(self):
        self.add(sample())
        self.add(sample(30, 1300))
        self.assertEqual(self.latest(120)["current_state"], "known")
        self.assertEqual(self.latest(121, replace(POLICY, stale_after_seconds=120))["current_state"], "known")

    def test_latest_failed_poll_does_not_return_an_older_good_sample(self):
        self.add(sample())
        absent = sample(30)
        self.store.ingest(poll(absent, "error", "synthetic_timeout"), [])
        latest = self.latest(40)
        self.assertEqual(latest["poll_id"], absent.poll_id)
        self.assertEqual(latest["current_state"], "unknown")
        self.assertEqual(latest["rate"]["reason"], "muestra_ausente")

    def test_latest_future_and_missing_data_remain_unknown(self):
        self.assertEqual(self.latest(0)["reason"], "sin_muestras")
        self.add(sample(30, 1300))
        self.assertEqual(self.latest(0)["current_state"], "unknown")

    def test_latest_handles_equal_timestamps_without_skipping_a_sample(self):
        self.add(sample())
        self.add(sample(30, 1300, poll_id="a"))
        self.add(sample(30, 1400, poll_id="b"))
        latest = self.latest(30)
        self.assertEqual(latest["rate"]["reason"], "intervalo_anormal")
        self.assertEqual(latest["rate"]["elapsed_seconds"], 0)

    def test_read_only_query_preserves_existing_data_schema_and_database_bytes(self):
        self.add(sample())
        self.add(sample(30, 1300))
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        with TelemetryStore(self.path, read_only=True) as reader:
            self.assertEqual(reader.latest("synthetic-device", "inventory-port", RX.name, POLICY,
                                           T0 + timedelta(seconds=31))["rate"]["state"], "inferida")
            self.assertEqual(reader.connection.execute("PRAGMA user_version").fetchone()[0], 1)
            with self.assertRaises(sqlite3.OperationalError):
                reader.ingest(poll(sample(60, 1600)), [sample(60, 1600)])
        self.assertEqual(before, hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertEqual(self.store.reconcile()["observation_rows"], 2)

    def test_read_only_missing_database_does_not_create_a_file(self):
        missing = Path(self.temp.name) / "not-created.sqlite3"
        with self.assertRaises(sqlite3.OperationalError):
            TelemetryStore(missing, read_only=True)
        self.assertFalse(missing.exists())

    def test_cli_inference_and_latest_are_offline_and_versioned(self):
        self.add(sample())
        self.add(sample(30, 1300))
        common = ["--database", str(self.path), "--device", "synthetic-device",
                  "--interface", "inventory-port", "--metric", RX.name]
        with redirect_stdout(io.StringIO()) as output:
            main(["history", *common, "--as-of", "2026-10-21T12:00:31Z",
                  "--start", "2026-10-21T12:00:00Z", "--end", "2026-10-21T12:00:31Z",
                  "--continuity-policy", "infer-64"])
        rate = json.loads(output.getvalue())[1]["rate"]
        self.assertEqual((rate["schema_version"], rate["state"], rate["value_bps"]), (2, "inferida", 80))
        with redirect_stdout(io.StringIO()) as output:
            main(["latest", *common, "--as-of", "2026-10-21T12:02:01Z"])
        result = json.loads(output.getvalue())
        self.assertEqual(result["current_state"], "unknown")
        self.assertEqual(result["rate"]["reason"], "dato_antiguo")


if __name__ == "__main__":
    unittest.main()
