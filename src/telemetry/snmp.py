"""Adaptador SNMP v2c de lectura; el núcleo y su contrato v1 permanecen offline."""

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
import ipaddress
import json
import math
import os
from pathlib import Path
import re
from uuid import uuid4

from .model import Metric, Observation, PollAttempt

SYS_DESCR = "1.3.6.1.2.1.1.1.0"
SYS_OBJECT_ID = "1.3.6.1.2.1.1.2.0"
SYS_UPTIME = "1.3.6.1.2.1.1.3.0"
IF_NAME = "1.3.6.1.2.1.31.1.1.1.1"
IF_MAC = "1.3.6.1.2.1.2.2.1.6"
IF_SPEED = "1.3.6.1.2.1.2.2.1.5"
IF_STATUS = "1.3.6.1.2.1.2.2.1.8"
IF_DISCONTINUITY = "1.3.6.1.2.1.31.1.1.1.19"
COUNTERS = {
    32: ("1.3.6.1.2.1.2.2.1.10", "1.3.6.1.2.1.2.2.1.16"),
    64: ("1.3.6.1.2.1.31.1.1.1.6", "1.3.6.1.2.1.31.1.1.1.10"),
}


class SnmpFailure(Exception):
    """Código sanitizado; nunca incorpora credenciales ni la excepción remota."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class SnmpSettings:
    host: str
    community: str = field(repr=False)
    port: int = 161
    version: str = "2c"
    timeout_seconds: float = 2
    retries: int = 1
    interface_index: int | None = None
    device_id: str = ""
    interface_id: str = ""
    interface_identity: str = ""
    expected_sys_object_id: str | None = None
    expected_interface_name: str | None = None
    expected_interface_mac: str | None = None
    counter_bits: int = 64

    def __post_init__(self):
        try:
            ipaddress.IPv4Address(self.host)
        except (ValueError, TypeError):
            raise ValueError("host debe ser una dirección IPv4 literal") from None
        if self.version != "2c":
            raise ValueError("Esta iteración implementa únicamente SNMP v2c")
        if not isinstance(self.community, str) or not self.community:
            raise ValueError("Falta community en el archivo local o KOKYANGWUTI_SNMP_COMMUNITY")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("port inválido")
        if type(self.retries) is not int or not 0 <= self.retries <= 2:
            raise ValueError("retries debe estar entre 0 y 2")
        if type(self.timeout_seconds) not in (int, float) or not math.isfinite(self.timeout_seconds) or not 0.1 <= self.timeout_seconds <= 10:
            raise ValueError("timeout_seconds debe estar entre 0.1 y 10")
        if self.interface_index is not None and (
            type(self.interface_index) is not int or not 1 <= self.interface_index < 2**31
        ):
            raise ValueError("interface_index inválido")
        if type(self.counter_bits) is not int or self.counter_bits not in (32, 64):
            raise ValueError("counter_bits debe ser 32 o 64")

    def require_binding(self):
        for name in ("device_id", "interface_id", "interface_identity",
                     "expected_sys_object_id", "expected_interface_name", "expected_interface_mac"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError("Falta vincular la interfaz en el archivo local: " + name)
        if self.interface_index is None:
            raise ValueError("Falta interface_index: primero inventory/inspect")
        if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)+", self.expected_sys_object_id):
            raise ValueError("expected_sys_object_id inválido")
        if not re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", self.expected_interface_mac):
            raise ValueError("expected_interface_mac debe tener seis octetos separados por ':'")

    @classmethod
    def load(cls, path):
        if str(path).startswith(("\\\\", "//")):
            raise ValueError("La configuración debe ser local")
        try:
            value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError:
            raise ValueError("JSON de configuración inválido; no se imprime su contenido") from None
        if not isinstance(value, dict) or set(value) - set(cls.__dataclass_fields__):
            raise ValueError("Configuración inválida o con campos desconocidos")
        value["community"] = os.getenv("KOKYANGWUTI_SNMP_COMMUNITY") or value.get("community", "")
        try:
            return cls(**value)
        except TypeError:
            raise ValueError("Faltan campos en la configuración local") from None


@dataclass(frozen=True)
class SnmpValue:
    kind: str
    value: int | str | bytes | None


@dataclass(frozen=True)
class SnmpResponse:
    values: dict[str, SnmpValue]
    received_at: datetime


class PySnmpClient:
    """Solo GET y GETNEXT. OIDs numéricos; no carga MIBs desde Internet."""

    def __init__(self, settings):
        self.settings = settings
        self.engine = None

    async def __aenter__(self):
        try:
            import pysnmp.hlapi.v3arch.asyncio as api
        except ImportError:
            raise SnmpFailure("snmp_dependency_missing") from None
        self.api = api
        try:
            self.engine = api.SnmpEngine()
            self.auth = api.CommunityData(self.settings.community, mpModel=1)
            self.target = await api.UdpTransportTarget.create(
                (self.settings.host, self.settings.port),
                timeout=self.settings.timeout_seconds, retries=self.settings.retries,
            )
        except Exception:
            if self.engine is not None:
                self.engine.close_dispatcher()
            raise SnmpFailure("snmp_transport_error") from None
        return self

    async def __aexit__(self, *_):
        self.engine.close_dispatcher()

    @staticmethod
    def convert(value):
        from pysnmp.proto import rfc1902, rfc1905
        if isinstance(value, (rfc1905.NoSuchObject, rfc1905.NoSuchInstance, rfc1905.EndOfMibView)):
            return SnmpValue("missing", None)
        for kind, cls in (("counter64", rfc1902.Counter64), ("counter32", rfc1902.Counter32),
                          ("ticks", rfc1902.TimeTicks), ("gauge", rfc1902.Gauge32),
                          ("integer", rfc1902.Integer32)):
            if value.tagSet == cls.tagSet:
                return SnmpValue(kind, int(value))
        if value.tagSet == rfc1902.OctetString.tagSet:
            return SnmpValue("octets", bytes(value.asOctets()))
        if value.tagSet == rfc1902.ObjectIdentifier.tagSet:
            return SnmpValue("oid", str(value))
        return SnmpValue("unsupported_type", None)

    async def _request(self, oids, next_oid=False):
        from pysnmp.proto import errind
        command = self.api.next_cmd if next_oid else self.api.get_cmd
        try:
            response = await asyncio.wait_for(
                command(self.engine, self.auth, self.target, self.api.ContextData(),
                        *(self.api.ObjectType(self.api.ObjectIdentity(oid)) for oid in oids),
                        lookupMib=False),
                timeout=self.settings.timeout_seconds * (self.settings.retries + 1) + 2,
            )
            received_at = datetime.now(UTC)
            indication, status, _, bindings = response
            if indication:
                code = "snmp_timeout" if indication == errind.requestTimedOut else "snmp_protocol_error"
                raise SnmpFailure(code)
            if status:
                raise SnmpFailure("snmp_access_denied" if int(status) in (6, 16) else "snmp_agent_error")
            values = {str(oid): self.convert(value) for oid, value in bindings}
            if len(values) != len(oids) or (not next_oid and set(values) != set(oids)):
                raise SnmpFailure("snmp_invalid_response")
            return SnmpResponse(values, received_at)
        except TimeoutError:
            raise SnmpFailure("snmp_timeout") from None
        except SnmpFailure:
            raise
        except Exception:
            raise SnmpFailure("snmp_transport_error") from None

    async def get(self, oids):
        return await self._request(tuple(oids))

    async def next(self, oid):
        return await self._request((oid,), next_oid=True)


def text_value(value):
    if value and value.kind == "octets" and isinstance(value.value, bytes):
        return value.value.decode("utf-8", errors="replace")
    return None


def number_value(value, kind, maximum):
    if value and value.kind == kind and type(value.value) is int and 0 <= value.value <= maximum:
        return value.value
    return None


def mac_value(value):
    if value and value.kind == "octets" and isinstance(value.value, bytes) and len(value.value) == 6:
        return value.value.hex(":")
    return None


async def inventory(client, limit=16):
    if type(limit) is not int or not 1 <= limit <= 64:
        raise ValueError("limit debe estar entre 1 y 64")
    system = await client.get((SYS_DESCR, SYS_OBJECT_ID, SYS_UPTIME))
    interfaces, cursor = [], IF_NAME
    for _ in range(limit):
        response = await client.next(cursor)
        oid, value = next(iter(response.values.items()))
        if value.kind == "missing" or not oid.startswith(IF_NAME + "."):
            break
        suffix = oid[len(IF_NAME) + 1:]
        if not suffix.isdigit() or tuple(map(int, oid.split("."))) <= tuple(map(int, cursor.split("."))):
            raise SnmpFailure("snmp_invalid_response")
        name = text_value(value)
        if not name:
            raise SnmpFailure("snmp_invalid_interface_name")
        interfaces.append({"interface_index": int(suffix), "interface_name": name})
        cursor = oid
    return {"description": text_value(system.values.get(SYS_DESCR)),
            "interfaces": interfaces, "limit_reached": len(interfaces) == limit}


async def inspect_interface(client, index):
    if type(index) is not int or not 1 <= index < 2**31:
        raise ValueError("interface_index inválido")
    oids = (SYS_DESCR, SYS_OBJECT_ID, SYS_UPTIME,
            *(f"{base}.{index}" for base in (IF_NAME, IF_MAC, IF_SPEED, IF_STATUS, IF_DISCONTINUITY)),
            *(f"{base}.{index}" for bits in (32, 64) for base in COUNTERS[bits]))
    response = await client.get(oids)
    values = response.values
    object_id = values.get(SYS_OBJECT_ID)
    supported = [bits for bits in (64, 32) if all(
        number_value(values.get(f"{base}.{index}"), f"counter{bits}", 2**bits - 1) is not None
        for base in COUNTERS[bits]
    )]
    return {
        "description": text_value(values.get(SYS_DESCR)),
        "interface_index": index,
        "expected_sys_object_id": object_id.value if object_id and object_id.kind == "oid" else None,
        "expected_interface_name": text_value(values.get(f"{IF_NAME}.{index}")),
        "expected_interface_mac": mac_value(values.get(f"{IF_MAC}.{index}")),
        "counter_bits_supported": supported,
        "speed_bps": number_value(values.get(f"{IF_SPEED}.{index}"), "gauge", 2**32 - 1),
        "oper_status": number_value(values.get(f"{IF_STATUS}.{index}"), "integer", 7),
        "discontinuity_supported": number_value(values.get(f"{IF_DISCONTINUITY}.{index}"), "ticks", 2**32 - 1) is not None,
    }


@dataclass(frozen=True)
class Capture:
    attempt: PollAttempt
    observations: tuple[Observation, ...]
    warnings: tuple[str, ...] = ()


async def capture_once(settings, client, *, clock=lambda: datetime.now(UTC), poll_id=None):
    settings.require_binding()
    start = clock()
    poll_id = poll_id or str(uuid4())
    index = settings.interface_index
    metrics = (Metric("if_in_octets", "counter", "octets", settings.counter_bits),
               Metric("if_out_octets", "counter", "octets", settings.counter_bits),
               Metric("if_oper_status", "state", "enum"),
               Metric("if_speed", "gauge", "bits_per_second"))
    bases = (*COUNTERS[settings.counter_bits], IF_STATUS, IF_SPEED)
    oids = (SYS_OBJECT_ID, SYS_UPTIME,
            *(f"{base}.{index}" for base in (IF_NAME, IF_MAC, IF_DISCONTINUITY, *bases)))

    def attempt(end, status, code):
        return PollAttempt(poll_id, settings.device_id, settings.interface_id,
                           settings.interface_identity, start, end, metrics, status, code)

    try:
        response = await client.get(oids)
    except SnmpFailure as error:
        end = clock()
        if end < start:
            return Capture(attempt(start, "error", "collector_clock_regressed"), ())
        return Capture(attempt(end, "error", error.code), ())
    ingested = clock()
    if not start <= response.received_at <= ingested:
        return Capture(attempt(max(start, ingested), "error", "collector_clock_regressed"), ())
    values = response.values
    observed_name = text_value(values.get(f"{IF_NAME}.{index}"))
    observed_mac = mac_value(values.get(f"{IF_MAC}.{index}"))
    object_id = values.get(SYS_OBJECT_ID)
    bound = (object_id is not None and object_id.kind == "oid"
             and object_id.value == settings.expected_sys_object_id
             and observed_name == settings.expected_interface_name
             and observed_mac == settings.expected_interface_mac.lower())
    uptime = number_value(values.get(SYS_UPTIME), "ticks", 2**32 - 1)
    discontinuity = number_value(values.get(f"{IF_DISCONTINUITY}.{index}"), "ticks", 2**32 - 1)
    warnings = []
    if uptime is None:
        warnings.append("uptime_unavailable")
    if discontinuity is None:
        warnings.append("discontinuity_unavailable")
    observations = []
    for metric, base, kind, maximum in zip(
        metrics, bases, (f"counter{settings.counter_bits}", f"counter{settings.counter_bits}", "integer", "gauge"),
        (2**settings.counter_bits - 1, 2**settings.counter_bits - 1, 7, 2**32 - 1),
    ):
        source = values.get(f"{base}.{index}")
        value = number_value(source, kind, maximum)
        if metric.name == "if_oper_status" and value == 0:
            value = None
        quality = "good" if value is not None else "missing" if source is None or source.kind == "missing" else "error"
        if not bound:
            value, quality = None, "unknown"
        observations.append(Observation(
            poll_id=poll_id, device_id=settings.device_id, interface_id=settings.interface_id,
            interface_identity=settings.interface_identity, metric=metric, raw_value=value,
            queried_at=response.received_at, ingested_at=ingested, observed_at=None,
            quality=quality, uptime_ticks=uptime, discontinuity_ticks=discontinuity,
            interface_index=index, interface_name=observed_name,
        ))
    good = sum(item.quality == "good" for item in observations)
    status = "success" if good == len(metrics) else "partial" if good else "error"
    code = None if status == "success" else "snmp_metric_unavailable" if bound else "interface_binding_mismatch"
    return Capture(attempt(ingested, status, code), tuple(observations), tuple(warnings))
