from __future__ import annotations

import json
import os
import re
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pvian_gateway.profiles import ProviderProfile


class QualificationStateError(RuntimeError):
    """The local, redacted qualification state is invalid."""


# A route enters the requalification set once its remaining validity drops to
# this window, so the daily 04:00 checkpoint renews routes to a fresh 168h
# TTL *before* they expire instead of after (operator directive 2026-08-20).
RENEWAL_WINDOW_HOURS = 24


class UsageAttemptLedger:
    """Append-only local accounting observations; no request/response text is stored."""

    def __init__(self, state_file: Path) -> None:
        self.path = state_file.with_name("usage-attempt-ledger.json")
        self.lock_path = self.path.with_name(f"{self.path.name}.lock")
        self._lock = Lock()

    def record(self, entry: dict[str, Any]) -> None:
        if not _safe_usage_attempt(entry):
            raise QualificationStateError("usage attempt entry is invalid or unsafe")
        with self._lock:
            with _interprocess_file_lock(self.lock_path):
                payload = self._read()
                payload["attempts"].append(entry)
                self._write(payload)

    def list_attempts(self) -> list[dict[str, Any]]:
        with self._lock:
            with _interprocess_file_lock(self.lock_path):
                return list(self._read()["attempts"])

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": "pvian.usage-attempt-ledger/v1", "attempts": []}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise QualificationStateError("cannot read usage attempt ledger") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != "pvian.usage-attempt-ledger/v1"
            or not isinstance(payload.get("attempts"), list)
            or set(payload) != {"schema_version", "attempts"}
            or not all(_safe_usage_attempt(item) for item in payload.get("attempts", []))
        ):
            raise QualificationStateError("usage attempt ledger structure is invalid")
        return payload

    def _write(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=self.path.parent, delete=False
            ) as temporary:
                json.dump(payload, temporary, ensure_ascii=False, indent=2, sort_keys=True)
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_path = Path(temporary.name)
            os.replace(temporary_path, self.path)
        except Exception:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise


class QualificationStateStore:
    """Atomic local store shared by the gateway and an explicitly run qualifier.

    It deliberately stores only metadata and summary fields.  Callers must not
    supply raw provider content or exception text to this type.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock_path = path.with_name(f"{path.name}.lock")
        self._lock = Lock()

    def list_records(self) -> list[dict[str, Any]]:
        with self._locked_payload() as payload:
            return [payload["records"][key] for key in sorted(payload["records"])]

    def get(self, route_id: str) -> dict[str, Any] | None:
        with self._locked_payload() as payload:
            return payload["records"].get(route_id)

    def record_qualification(
        self,
        *,
        route_id: str,
        profile_id: str,
        profile_version: str,
        provider_id: str,
        endpoint_id: str,
        requested_model: str,
        reported_model: str | None,
        reported_model_identity: dict[str, str] | None = None,
        passed: bool,
        capability_summary: dict[str, Any],
        validity_hours: int,
        failure_threshold: int,
        observed_at: datetime | None = None,
    ) -> dict[str, Any]:
        now = observed_at or datetime.now(UTC)
        _require_utc_datetime(now)
        identity = _normalize_reported_model_identity(
            requested_model, reported_model, reported_model_identity
        )
        enabled = passed and _reported_model_identity_is_accepted(identity)
        record = {
            "route_id": route_id,
            "profile_id": profile_id,
            "profile_version": profile_version,
            "provider_id": provider_id,
            "endpoint_id": endpoint_id,
            "requested_model": requested_model,
            "reported_model": reported_model,
            "reported_model_identity": identity,
            "qualification_status": "enabled" if enabled else "candidate",
            "operational_status": "nominal" if enabled else "unqualified",
            "qualified_at": _timestamp(now) if enabled else None,
            "valid_until": _timestamp(now + timedelta(hours=validity_hours)) if enabled else None,
            "validity_hours": validity_hours,
            "consecutive_failure_threshold": failure_threshold,
            "consecutive_failures": 0,
            "revalidation_due": not enabled,
            "last_observed_at": _timestamp(now),
            "last_success_at": None,
            "last_failure_at": None,
            "last_failure_category": None,
            "request_count": 0,
            "usage_totals": {
                "prompt_tokens": 0,
                "cached_tokens": 0,
                "reasoning_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            },
            "known_usage_attempts": 0,
            "capabilities": capability_summary,
        }
        with self._locked_payload() as payload:
            previous = payload["records"].get(route_id)
            _merge_runtime_observations(record, previous, now)
            payload["records"][route_id] = record
            self._write(payload)
        return record

    def record_success(
        self,
        route_id: str,
        usage: dict[str, int | None] | None = None,
        observed_at: datetime | None = None,
    ) -> dict[str, Any] | None:
        now = observed_at or datetime.now(UTC)
        _require_utc_datetime(now)
        with self._locked_payload() as payload:
            record = payload["records"].get(route_id)
            if record is None:
                return None
            record["consecutive_failures"] = 0
            record["last_success_at"] = _timestamp(now)
            record["last_observed_at"] = _timestamp(now)
            # B: clear failure category AND timestamp as a pair to avoid
            # state self-corruption after a 502 → 200 recovery.
            record["last_failure_category"] = None
            record["last_failure_at"] = None
            record["request_count"] = int(record.get("request_count", 0)) + 1
            # D: track known-usage attempts separately so request_count>0 with
            # zero tokens is not misread as zero consumption.
            if usage is not None:
                record["known_usage_attempts"] = int(record.get("known_usage_attempts", 0)) + 1
            _add_usage(record["usage_totals"], usage)
            self._write(payload)
            return record

    def record_failure(
        self,
        route_id: str,
        category: str,
        observed_at: datetime | None = None,
    ) -> dict[str, Any] | None:
        now = observed_at or datetime.now(UTC)
        _require_utc_datetime(now)
        with self._locked_payload() as payload:
            record = payload["records"].get(route_id)
            if record is None:
                return None
            failures = int(record.get("consecutive_failures", 0)) + 1
            record["consecutive_failures"] = failures
            record["last_failure_category"] = category
            record["last_failure_at"] = _timestamp(now)
            record["last_observed_at"] = _timestamp(now)
            record["request_count"] = int(record.get("request_count", 0)) + 1
            if failures >= int(record["consecutive_failure_threshold"]):
                record["operational_status"] = "degraded"
                record["revalidation_due"] = True
            self._write(payload)
            return record

    def callable_record(self, route_id: str, now: datetime | None = None) -> dict[str, Any] | None:
        record = self.get(route_id)
        if record is None or record.get("qualification_status") != "enabled":
            return None
        if not _record_identity_is_accepted(record):
            return None
        if record.get("revalidation_due"):
            return None
        valid_until = record.get("valid_until")
        current = now or datetime.now(UTC)
        if not _is_utc_datetime(current) or not isinstance(valid_until, str):
            return None
        if _parse_timestamp(valid_until) <= current:
            return None
        return record

    def validate_profile_bindings(self, profiles: dict[str, ProviderProfile]) -> None:
        """Validate state/profile bindings without exposing state contents.

        Startup calls this before the gateway accepts requests.  Every record
        must bind exactly to a qualification-enabled endpoint; enabled records
        additionally require an active profile and exact provider-reported model.
        """

        with self._locked_payload() as payload:
            for stored_route_id, record in payload["records"].items():
                valid = (
                    isinstance(stored_route_id, str)
                    and isinstance(record, dict)
                    and _record_matches_profile(stored_route_id, record, profiles)
                )
                if not valid:
                    raise QualificationStateError("qualification state record binding is invalid")

    def due_records(self, now: datetime | None = None) -> list[dict[str, Any]]:
        current = now or datetime.now(UTC)
        if not _is_utc_datetime(current):
            raise QualificationStateError("qualification state comparison time is invalid")
        result: list[dict[str, Any]] = []
        for record in self.list_records():
            valid_until = record.get("valid_until")
            expired = not isinstance(valid_until, str) or _parse_timestamp(valid_until) <= current
            within_window = (
                isinstance(valid_until, str)
                and not expired
                and (_parse_timestamp(valid_until) - current)
                <= timedelta(hours=RENEWAL_WINDOW_HOURS)
            )
            if record.get("revalidation_due") or expired or within_window:
                result.append(record)
        return result

    def renewal_due(self, route_id: str, now: datetime | None = None) -> bool:
        """True when the daily requalifier should refresh this route now.

        Mirrors due_records semantics for a single route: unknown/inconclusive
        records, degraded records, expired records, and records whose remaining
        validity has entered RENEWAL_WINDOW_HOURS are due. This never gates the
        request path — callable_record keeps serving until actual expiry.
        """
        record = self.get(route_id)
        if record is None or record.get("qualification_status") != "enabled":
            return True
        if record.get("revalidation_due"):
            return True
        valid_until = record.get("valid_until")
        current = now or datetime.now(UTC)
        if not _is_utc_datetime(current) or not isinstance(valid_until, str):
            return True
        remaining = _parse_timestamp(valid_until) - current
        return remaining <= timedelta(0) or remaining <= timedelta(hours=RENEWAL_WINDOW_HOURS)

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": "pvian.qualification-state/v1", "records": {}}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise QualificationStateError("cannot read qualification state") from exc
        if not isinstance(payload, dict):
            raise QualificationStateError("qualification state payload must be an object")
        if payload.get("schema_version") != "pvian.qualification-state/v1":
            raise QualificationStateError("unsupported qualification state schema")
        if not isinstance(payload.get("records"), dict):
            raise QualificationStateError("qualification state records must be an object")
        if set(payload) != {"schema_version", "records"} or not _state_records_are_safe(
            payload["records"]
        ):
            raise QualificationStateError("qualification state structure is invalid")
        return payload

    @contextmanager
    def _locked_payload(self) -> Iterator[dict[str, Any]]:
        """Protect a short state-file read/modify/write critical section.

        A qualifier is deliberately a separate process from the gateway.  The
        per-instance thread lock alone is therefore insufficient; the adjacent
        lock file provides cross-process exclusion on Windows and POSIX.  No
        provider request is made while this context is held.
        """

        with self._lock:
            with _interprocess_file_lock(self.lock_path):
                yield self._read()

    @contextmanager
    def _locked_raw_payload(self) -> Iterator[dict[str, Any]]:
        """Lock + restricted raw-JSON read (envelope-only, no per-record check).

        Used by ``repair_state`` so that the very issues being repaired (which
        ``_read`` would reject) can be loaded safely.  The envelope is still
        validated: bad JSON, wrong schema_version, or extra top-level keys
        are rejected here.
        """

        with self._lock:
            with _interprocess_file_lock(self.lock_path):
                yield self._read_raw()

    def _read_raw(self) -> dict[str, Any]:
        """Read JSON with envelope-only validation; skip per-record safety."""

        if not self.path.exists():
            return {"schema_version": "pvian.qualification-state/v1", "records": {}}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise QualificationStateError("cannot read qualification state") from exc
        if not isinstance(payload, dict):
            raise QualificationStateError("qualification state payload must be an object")
        if payload.get("schema_version") != "pvian.qualification-state/v1":
            raise QualificationStateError("unsupported qualification state schema")
        if not isinstance(payload.get("records"), dict):
            raise QualificationStateError("qualification state records must be an object")
        if set(payload) != {"schema_version", "records"}:
            raise QualificationStateError("qualification state structure is invalid")
        return payload

    def repair_state(self) -> dict[str, int]:
        """Explicitly repair known historical compatibility issues.

        This is an offline, network-free entry point.  It reads the state
        with a restricted raw-JSON reader (envelope-only, not per-record
        validation) inside the cross-process lock, applies only two known
        historical fixes, writes a backup, and rejects any unknown, sensitive,
        or structurally damaged state.

        Known repairs:
          1. Legacy 3-key ``usage_totals`` → canonical 5-key.
          2. ``last_failure_category`` is ``None`` and ``last_failure_at`` is
             a valid UTC timestamp → clear ``last_failure_at`` to ``None``
             (fixes the pre-fix-B self-corruption where ``record_success``
             only cleared the category but not the timestamp).

        Unknown fields, sensitive fields, bad envelope, bad timestamps, or
        other relation errors are rejected; the original file is not modified.
        It must not be called automatically at gateway startup.

        Returns a summary with counts of ``records_repaired`` and
        ``records_unchanged``.  No state body, credentials, prompts, or
        provider responses are output.
        """

        with self._locked_raw_payload() as payload:
            repaired = 0
            unchanged = 0
            for _route_id, record in payload["records"].items():
                # Guard: non-dict records are structural damage; let
                # _validate_state_payload reject them below.
                if not isinstance(record, dict):
                    unchanged += 1
                    continue
                touched = False
                # Repair 1: legacy 3-key → canonical 5-key.
                usage = record.get("usage_totals")
                if isinstance(usage, dict) and set(usage) == _USAGE_TOTAL_KEYS_LEGACY:
                    # Do not coerce malformed persisted values (for example
                    # bools, strings, or floats) into valid-looking tokens.
                    # Full validation below rejects them fail-closed.
                    if all(_is_nonnegative_int(value) for value in usage.values()):
                        record["usage_totals"] = {
                            **{key: 0 for key in _USAGE_TOTAL_KEYS},
                            **usage,
                        }
                        touched = True
                # Repair 2: failure category cleared but timestamp not.
                if (
                    record.get("last_failure_category") is None
                    and isinstance(record.get("last_failure_at"), str)
                    and _is_timestamp(record["last_failure_at"])
                ):
                    record["last_failure_at"] = None
                    touched = True
                if touched:
                    repaired += 1
                else:
                    unchanged += 1
            # Full validation: unknown/sensitive/structural damage is rejected.
            _validate_state_payload(payload)
            backup = self.path.with_suffix(".bak")
            if self.path.exists():
                backup.write_bytes(self.path.read_bytes())
            self._write(payload)
        return {"records_repaired": repaired, "records_unchanged": unchanged}

    def _write(self, payload: dict[str, Any]) -> None:
        # C: validate the full payload before replacing the original file.
        _validate_state_payload(payload)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=self.path.parent, delete=False
            ) as temporary:
                json.dump(payload, temporary, ensure_ascii=False, indent=2, sort_keys=True)
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_path = Path(temporary.name)
            os.replace(temporary_path, self.path)
        except Exception:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise


@contextmanager
def _interprocess_file_lock(path: Path) -> Iterator[None]:
    """Use a one-byte lock file so Windows and POSIX share the same contract."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.01)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _add_usage(target: dict[str, Any], usage: dict[str, int | None] | None) -> None:
    """Aggregate a normalized usage summary into state totals.

    Only non-negative ints are added; ``None`` (unknown) values are skipped so
    that a request with unknown usage does not dilute the aggregate to zero.
    Reasoning tokens are tracked separately and never double-counted into
    completion.
    """

    if usage is None:
        return
    for key in _USAGE_TOTAL_KEYS:
        if key not in target:
            continue
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            target[key] = int(target.get(key, 0)) + value


_RECORD_FIELDS_V1 = frozenset(
    {
        "route_id",
        "profile_id",
        "profile_version",
        "provider_id",
        "endpoint_id",
        "requested_model",
        "reported_model",
        "qualification_status",
        "operational_status",
        "qualified_at",
        "valid_until",
        "validity_hours",
        "consecutive_failure_threshold",
        "consecutive_failures",
        "revalidation_due",
        "last_observed_at",
        "last_success_at",
        "last_failure_at",
        "last_failure_category",
        "request_count",
        "usage_totals",
        "capabilities",
    }
)
# D: V2 adds known_usage_attempts so request_count>0 with zero tokens is
# not misread as zero consumption; legacy V1 records remain loadable.
_RECORD_FIELDS_V2 = _RECORD_FIELDS_V1 | {"known_usage_attempts"}
_RECORD_FIELDS_V3_LEGACY = _RECORD_FIELDS_V1 | {"reported_model_identity"}
_RECORD_FIELDS_V3 = _RECORD_FIELDS_V2 | {"reported_model_identity"}


def _validate_state_payload(payload: object) -> None:
    """C: pre-write self-validation; reject invalid or sensitive payloads.

    If validation fails the caller must not replace the original file.
    """

    if not isinstance(payload, dict):
        raise QualificationStateError("payload must be an object")
    if payload.get("schema_version") != "pvian.qualification-state/v1":
        raise QualificationStateError("unsupported qualification state schema")
    records = payload.get("records")
    if not isinstance(records, dict) or set(payload) != {"schema_version", "records"}:
        raise QualificationStateError("qualification state structure is invalid")
    if not _state_records_are_safe(records):
        raise QualificationStateError("qualification state records are invalid")


def _state_records_are_safe(records: dict[str, Any]) -> bool:
    return all(
        isinstance(route_id, str) and _state_record_is_safe(record)
        for route_id, record in records.items()
    )


def _state_record_is_safe(record: object) -> bool:
    if not isinstance(record, dict):
        return False
    fields = set(record)
    if fields not in {
        _RECORD_FIELDS_V1,
        _RECORD_FIELDS_V2,
        _RECORD_FIELDS_V3_LEGACY,
        _RECORD_FIELDS_V3,
    }:
        return False
    # D: known_usage_attempts must be a non-negative int not exceeding request_count.
    # Guard: request_count type is checked later (lines below); avoid
    # TypeError by only comparing when both are valid ints.
    if "known_usage_attempts" in record:
        kua = record["known_usage_attempts"]
        rc = record["request_count"]
        if (
            not isinstance(kua, int)
            or isinstance(kua, bool)
            or kua < 0
            or (isinstance(rc, int) and not isinstance(rc, bool) and kua > rc)
        ):
            return False
    identity_fields = (
        "route_id",
        "profile_id",
        "profile_version",
        "provider_id",
        "endpoint_id",
        "requested_model",
        "last_observed_at",
    )
    if any(not isinstance(record.get(name), str) or not record[name] for name in identity_fields):
        return False
    if record["qualification_status"] not in {"candidate", "enabled"}:
        return False
    if record["operational_status"] not in {"nominal", "unqualified", "degraded"}:
        return False
    reported_model = record["reported_model"]
    if reported_model is not None and (not isinstance(reported_model, str) or not reported_model):
        return False
    if "reported_model_identity" in record and not _reported_model_identity_is_safe(
        record["reported_model_identity"], record["requested_model"], reported_model
    ):
        return False
    if any(
        not isinstance(record[name], int) or isinstance(record[name], bool) or record[name] < 0
        for name in (
            "validity_hours",
            "consecutive_failure_threshold",
            "consecutive_failures",
            "request_count",
        )
    ):
        return False
    if record["validity_hours"] == 0 or record["consecutive_failure_threshold"] == 0:
        return False
    if not isinstance(record["revalidation_due"], bool) or not _safe_capabilities(
        record["capabilities"]
    ):
        return False
    if record["last_failure_category"] is not None and record["last_failure_category"] not in {
        "transport_error",
        "rate_limited",
        "upstream_5xx",
        "timeout",
        "upstream_protocol_or_auth",
        "upstream_error",
    }:
        return False
    if not _safe_timestamps(record) or not _safe_usage_totals(record["usage_totals"]):
        return False
    if record["request_count"] < record["consecutive_failures"]:
        return False
    if not _safe_state_relations(record):
        return False
    return record["qualification_status"] != "enabled" or _record_identity_is_accepted(record)


def _safe_timestamps(record: dict[str, Any]) -> bool:
    for name in ("qualified_at", "valid_until", "last_success_at", "last_failure_at"):
        value = record[name]
        if value is not None and (not isinstance(value, str) or not _is_timestamp(value)):
            return False
    if not _is_timestamp(record["last_observed_at"]):
        return False
    observed_at = _parse_timestamp(record["last_observed_at"])
    for name in ("last_success_at", "last_failure_at"):
        value = record[name]
        if value is not None and _parse_timestamp(value) > observed_at:
            return False
    return (record["last_failure_at"] is None) == (record["last_failure_category"] is None)


def _is_timestamp(value: str) -> bool:
    try:
        parsed = _parse_timestamp(value)
    except ValueError:
        return False
    return _timestamp(parsed) == value


def _is_utc_datetime(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() == timedelta(0)


def _require_utc_datetime(value: datetime) -> None:
    if not _is_utc_datetime(value):
        raise QualificationStateError("qualification state timestamp must be UTC-aware")


def _safe_state_relations(record: dict[str, Any]) -> bool:
    status = record["qualification_status"]
    if status == "candidate":
        return (
            record["qualified_at"] is None
            and record["valid_until"] is None
            and record["operational_status"] == "unqualified"
            and record["revalidation_due"] is True
        )
    if record["qualified_at"] is None or record["valid_until"] is None:
        return False
    qualified_at = _parse_timestamp(record["qualified_at"])
    valid_until = _parse_timestamp(record["valid_until"])
    if valid_until != qualified_at + timedelta(hours=record["validity_hours"]):
        return False
    if record["operational_status"] == "nominal":
        return (
            record["revalidation_due"] is False
            and record["consecutive_failures"] < record["consecutive_failure_threshold"]
        )
    return (
        record["operational_status"] == "degraded"
        and record["revalidation_due"] is True
        and record["consecutive_failures"] >= record["consecutive_failure_threshold"]
    )


_USAGE_TOTAL_KEYS_CANONICAL = frozenset(
    {"prompt_tokens", "cached_tokens", "reasoning_tokens", "completion_tokens", "total_tokens"}
)
_USAGE_TOTAL_KEYS_LEGACY = frozenset(
    {"prompt_tokens", "completion_tokens", "total_tokens"}
)
_USAGE_TOTAL_KEYS = tuple(sorted(_USAGE_TOTAL_KEYS_CANONICAL))


def _safe_usage_totals(value: object) -> bool:
    """Accept the canonical 5-key or legacy 3-key usage totals.

    Legacy 3-key state remains loadable; new writes always use 5 keys.
    """

    if not isinstance(value, dict):
        return False
    keys = set(value)
    if keys != _USAGE_TOTAL_KEYS_CANONICAL and keys != _USAGE_TOTAL_KEYS_LEGACY:
        return False
    return all(
        isinstance(item, int) and not isinstance(item, bool) and item >= 0
        for item in value.values()
    )


_SUMMARY_STATUSES = frozenset({"observed_supported", "inconclusive", "not_observed"})
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")


def _safe_capabilities(value: object) -> bool:
    """Accept only the redacted summary produced by the qualifier, or an empty fixture."""

    if value == {}:
        return True
    if not isinstance(value, dict) or not value or not set(value) <= {
        "message",
        "stream",
        "thinking",
        "native_tool_call",
        "assistant_replay",
        "tool_result_replay",
        "usage",
        "configuration_digest",
    }:
        return False
    validators = {
        "message": lambda item: _safe_probe_result(item, stream=False),
        "stream": lambda item: _safe_probe_result(item, stream=True),
        "thinking": _safe_thinking_summary,
        "native_tool_call": lambda item: _safe_tool_summary(item, native=True),
        "assistant_replay": lambda item: _safe_tool_summary(item, native=False),
        "tool_result_replay": lambda item: _safe_tool_summary(item, native=False),
        "usage": _safe_usage,
        "configuration_digest": _is_sha256,
    }
    return all(
        _safe_status_only(item) or validators[name](item) for name, item in value.items()
    )


def _safe_status_only(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"status"}
        and value["status"] in _SUMMARY_STATUSES
    )


def _safe_probe_result(value: object, *, stream: bool) -> bool:
    if not isinstance(value, dict) or value.get("status") not in _SUMMARY_STATUSES:
        return False
    fields = {"status", "output_length", "output_sha256", "finish_reason"}
    if stream:
        fields.add("chunk_count")
    if value["status"] == "inconclusive" and set(value) == {
        "status",
        "error_category",
    }:
        return _safe_category(value["error_category"])
    # A completed probe can still be inconclusive when the provider does not
    # reproduce the synthetic sentinel exactly. Preserve its bounded audit
    # fields instead of either dropping them or pretending the probe passed.
    if value["status"] not in {"observed_supported", "inconclusive"}:
        return False
    return (
        set(value) == fields
        and _is_nonnegative_int(value["output_length"])
        and _is_sha256(value["output_sha256"])
        and _safe_finish_reason(value["finish_reason"])
        and (not stream or _is_nonnegative_int(value["chunk_count"]))
    )


def _safe_thinking_summary(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {"status", "variants", "reported_model"}:
        return False
    return (
        value["status"] in _SUMMARY_STATUSES
        and isinstance(value["variants"], list)
        and all(_safe_thinking_variant(item) for item in value["variants"])
        and _safe_optional_model(value["reported_model"])
    )


def _safe_thinking_variant(value: object) -> bool:
    if not isinstance(value, dict) or not isinstance(value.get("variant_id"), str):
        return False
    if value.get("request_completed") is False:
        return set(value) == {
            "variant_id",
            "request_completed",
            "error_category",
        } and _safe_category(
            value["error_category"]
        )
    return (
        set(value)
        == {
            "variant_id",
            "request_completed",
            "reasoning_field_present",
            "reasoning_field_names",
            "reasoning_tokens",
            "usage_present",
        }
        and value["request_completed"] is True
        and isinstance(value["reasoning_field_present"], bool)
        and isinstance(value["reasoning_field_names"], list)
        and all(
            isinstance(name, str) and len(name) <= 64
            for name in value["reasoning_field_names"]
        )
        and (value["reasoning_tokens"] is None or _is_nonnegative_int(value["reasoning_tokens"]))
        and isinstance(value["usage_present"], bool)
    )


def _safe_tool_summary(value: object, *, native: bool) -> bool:
    if not isinstance(value, dict) or value.get("status") not in _SUMMARY_STATUSES:
        return False
    if value["status"] == "inconclusive":
        return set(value) == {"status", "error_category"} and _safe_category(
            value["error_category"]
        )
    if not native:
        return set(value) == {"status"}
    return (
        set(value) == {"status", "tool_call_count", "call_id_sha256"}
        and _is_nonnegative_int(value["tool_call_count"])
        and (value["call_id_sha256"] is None or _is_sha256(value["call_id_sha256"]))
    )


def _safe_usage(value: object) -> bool:
    """Accept canonical 5-key or legacy 3-key capability usage summaries.

    ``cached_tokens``, ``reasoning_tokens``, and ``total_tokens`` may be
    ``None`` (provider did not report them).  ``prompt_tokens`` and
    ``completion_tokens`` must be non-negative ints.
    """

    if value is None:
        return True
    if not isinstance(value, dict):
        return False
    keys = set(value)
    if keys != _USAGE_TOTAL_KEYS_CANONICAL and keys != _USAGE_TOTAL_KEYS_LEGACY:
        return False
    for name in keys:
        item = value[name]
        if name in ("cached_tokens", "reasoning_tokens", "total_tokens"):
            if item is not None and not _is_nonnegative_int(item):
                return False
        elif not _is_nonnegative_int(item):
            return False
    return True


def _safe_optional_model(value: object) -> bool:
    return value is None or (isinstance(value, str) and 0 < len(value) <= 256)


def _safe_category(value: object) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.]{0,127}", value) is not None
    )


def _safe_finish_reason(value: object) -> bool:
    return value is None or (
        isinstance(value, str)
        and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value) is not None
    )


_USAGE_ATTEMPT_FIELDS_V1 = frozenset(
    {
        "recorded_at",
        "request_id",
        "logical_call_id",
        "route_id",
        "provider",
        "region",
        "requested_model",
        "reported_model",
        "status",
        "stream",
        "usage",
        "cost_status",
        "estimated_list_cost_cny",
        "pricing_version",
        "estimate",
    }
)
_USAGE_ATTEMPT_FIELDS_V2 = _USAGE_ATTEMPT_FIELDS_V1 | {
    "attempt_count",
    "upstream_request_id_sha256",
    "upstream_status_code",
    "finish_reason",
    "error_category",
    "elapsed_ms",
}
_ATTEMPT_STATUSES = {"completed", "stream_interrupted", "transport_error", "upstream_error"}
_ATTEMPT_ERROR_CATEGORIES = {
    "transport_error",
    "rate_limited",
    "upstream_5xx",
    "timeout",
    "upstream_protocol_or_auth",
    "upstream_error",
}


def _safe_usage_attempt(value: object) -> bool:
    """Strict allowlist for terminal accounting evidence.

    Both the legacy shape already present on disk and the richer v2 entry are
    accepted.  Neither shape permits prompts, completions, raw provider bodies,
    remote error text, credentials, or private reasoning.
    """

    if not isinstance(value, dict) or set(value) not in {
        _USAGE_ATTEMPT_FIELDS_V1,
        _USAGE_ATTEMPT_FIELDS_V2,
    }:
        return False
    try:
        recorded = _parse_timestamp(value["recorded_at"])
    except (KeyError, TypeError, ValueError):
        return False
    if not _is_utc_datetime(recorded) or value["recorded_at"] != _timestamp(recorded):
        return False
    for name, limit in (
        ("request_id", 256),
        ("route_id", 512),
        ("provider", 128),
        ("region", 128),
        ("requested_model", 256),
    ):
        item = value.get(name)
        if not isinstance(item, str) or not item or len(item) > limit:
            return False
    if value.get("logical_call_id") is not None and (
        not isinstance(value["logical_call_id"], str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value["logical_call_id"])
        is None
    ):
        return False
    if not _safe_optional_model(value.get("reported_model")):
        return False
    if value.get("status") not in _ATTEMPT_STATUSES or not isinstance(value.get("stream"), bool):
        return False
    if not _safe_usage(value.get("usage")) or value.get("cost_status") not in {
        "estimated",
        "unknown",
    }:
        return False
    amount = value.get("estimated_list_cost_cny")
    if amount is not None and (
        not isinstance(amount, str) or re.fullmatch(r"\d+(?:\.\d+)?", amount) is None
    ):
        return False
    pricing = value.get("pricing_version")
    if pricing is not None and (not isinstance(pricing, str) or not pricing or len(pricing) > 128):
        return False
    if not _safe_attempt_estimate(value.get("estimate")):
        return False
    if set(value) == _USAGE_ATTEMPT_FIELDS_V1:
        return True
    if value.get("attempt_count") != 1:
        return False
    upstream_hash = value.get("upstream_request_id_sha256")
    if upstream_hash is not None and not _is_sha256(upstream_hash):
        return False
    status_code = value.get("upstream_status_code")
    if status_code is not None and (
        not isinstance(status_code, int)
        or isinstance(status_code, bool)
        or not 100 <= status_code <= 599
    ):
        return False
    if not _safe_finish_reason(value.get("finish_reason")):
        return False
    if value.get("error_category") is not None and value["error_category"] not in (
        _ATTEMPT_ERROR_CATEGORIES
    ):
        return False
    elapsed = value.get("elapsed_ms")
    if elapsed is not None and not _is_nonnegative_int(elapsed):
        return False
    status = value["status"]
    status_code = value["upstream_status_code"]
    error = value["error_category"]
    usage = value["usage"]
    finish = value["finish_reason"]
    upstream_hash = value["upstream_request_id_sha256"]
    reported = value["reported_model"]
    if status == "completed":
        return isinstance(status_code, int) and 200 <= status_code < 300 and error is None
    if status == "stream_interrupted":
        return (
            isinstance(status_code, int)
            and 200 <= status_code < 300
            and error == "upstream_error"
            and usage is None
        )
    if status == "transport_error":
        return (
            status_code is None
            and upstream_hash is None
            and reported is None
            and finish is None
            and usage is None
            and error == "transport_error"
        )
    return (
        isinstance(status_code, int)
        and not 200 <= status_code < 300
        and reported is None
        and finish is None
        and usage is None
        and error in _ATTEMPT_ERROR_CATEGORIES
    )


def _safe_attempt_estimate(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    if value.get("status") == "unknown":
        return set(value) == {"status", "reason"} and value.get("reason") in {
            "usage_unknown",
            "pricing_missing",
            "pricing_tier_missing",
            "cached_input_pricing_unknown",
            "reported_model_mismatch",
        }
    return (
        value.get("status") == "estimated"
        and set(value) == {"status", "currency", "pricing_version", "effective_at", "amount"}
        # CNY (Bailian, Moonshot official) and USD (DeepSeek official, OpenRouter)
        # are the frozen local-pricing currencies; anything else is rejected.
        and value.get("currency") in {"CNY", "USD"}
        and isinstance(value.get("pricing_version"), str)
        and (value.get("effective_at") is None or isinstance(value.get("effective_at"), str))
        and isinstance(value.get("amount"), str)
        and re.fullmatch(r"\d+(?:\.\d+)?", value["amount"]) is not None
    )


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _is_nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _record_matches_profile(
    stored_route_id: str, record: dict[str, Any], profiles: dict[str, ProviderProfile]
) -> bool:
    """Return whether a state record has a complete exact profile binding."""

    required = (
        "route_id",
        "profile_id",
        "profile_version",
        "provider_id",
        "endpoint_id",
        "requested_model",
    )
    if any(not isinstance(record.get(name), str) or not record[name] for name in required):
        return False
    reported_model = record.get("reported_model")
    if reported_model is not None and (not isinstance(reported_model, str) or not reported_model):
        return False
    status = record.get("qualification_status")
    if status not in {"candidate", "enabled"}:
        return False
    if record["route_id"] != stored_route_id:
        return False
    profile = profiles.get(record["profile_id"])
    if (
        profile is None
        or profile.profile_version != record["profile_version"]
        or profile.provider_id != record["provider_id"]
        or profile.policy.validity_hours != record["validity_hours"]
        or profile.policy.consecutive_failure_threshold
        != record["consecutive_failure_threshold"]
        or (status == "enabled" and profile.lifecycle != "active")
    ):
        return False
    endpoint = next(
        (item for item in profile.endpoints if item.endpoint_id == record["endpoint_id"]), None
    )
    if (
        endpoint is None
        or endpoint.transport
        not in {
            "openai_compatible_chat_completions",
            "anthropic_native_messages",
        }
        or endpoint.qualification_enabled is not True
    ):
        return False
    requested_model = record["requested_model"]
    decision = profile.reported_model_decision(requested_model, reported_model)
    stored_identity = record.get("reported_model_identity")
    if status == "enabled":
        if not decision.accepted:
            return False
        if stored_identity is None:
            if decision.match != "exact":
                return False
        elif stored_identity != decision.as_dict():
            return False
    return (
        profile.declares_upstream_model(requested_model)
        and record["route_id"] == f"{endpoint.route_id_prefix}:{requested_model}"
    )


def _normalize_reported_model_identity(
    requested_model: str,
    reported_model: str | None,
    value: dict[str, str] | None,
) -> dict[str, str]:
    if value is None:
        value = {
            "schema_version": "pvian.reported-model-identity-decision/v1",
            "policy_id": "pvian.reported-model-identity/exact-v1",
            "match": "exact" if reported_model == requested_model else "rejected",
        }
    if not _reported_model_identity_is_safe(value, requested_model, reported_model):
        raise QualificationStateError("reported model identity decision is invalid")
    return dict(value)


def _reported_model_identity_is_safe(
    value: object, requested_model: str, reported_model: str | None
) -> bool:
    if not isinstance(value, dict) or set(value) != {"schema_version", "policy_id", "match"}:
        return False
    if value.get("schema_version") != "pvian.reported-model-identity-decision/v1":
        return False
    if not isinstance(value.get("policy_id"), str) or not value["policy_id"]:
        return False
    match = value.get("match")
    if match not in {"exact", "explicit_mapping", "rejected"}:
        return False
    if match == "exact":
        return reported_model == requested_model
    if match == "explicit_mapping":
        return reported_model is not None and reported_model != requested_model
    return reported_model != requested_model


def _reported_model_identity_is_accepted(value: object) -> bool:
    return isinstance(value, dict) and value.get("match") in {"exact", "explicit_mapping"}


def _record_identity_is_accepted(record: dict[str, Any]) -> bool:
    value = record.get("reported_model_identity")
    if value is None:
        return record.get("reported_model") == record.get("requested_model")
    return _reported_model_identity_is_accepted(value)


def reported_model_matches_state(record: dict[str, Any], reported_model: str | None) -> bool:
    """Match runtime evidence against the exact identity qualified in state."""

    if reported_model == record.get("requested_model"):
        return True
    return bool(
        reported_model is not None
        and reported_model == record.get("reported_model")
        and isinstance(record.get("reported_model_identity"), dict)
        and record["reported_model_identity"].get("match") == "explicit_mapping"
    )


def _merge_runtime_observations(
    qualification: dict[str, Any], previous: dict[str, Any] | None, observed_at: datetime
) -> None:
    """Retain aggregate and post-qualification gateway observations.

    Qualification can race a completed gateway request in another process.  It
    is allowed to reset failures that happened before its observation, but must
    not erase request counts, usage, or failures recorded afterwards.
    """

    if previous is None:
        return
    qualification["request_count"] = int(previous.get("request_count", 0))
    # D: preserve known_usage_attempts from previous so the aggregate is not lost.
    qualification["known_usage_attempts"] = int(previous.get("known_usage_attempts", 0))
    previous_usage = previous.get("usage_totals")
    if isinstance(previous_usage, dict):
        # Preserve whichever key set the previous state had; new records
        # always start with 5 keys, so only copy keys that exist.
        qualification["usage_totals"] = {
            key: int(previous_usage.get(key, 0))
            for key in _USAGE_TOTAL_KEYS
            if key in previous_usage
        }
    last_observed = previous.get("last_observed_at")
    if not isinstance(last_observed, str) or _parse_timestamp(last_observed) < observed_at:
        return
    for key in ("last_observed_at", "last_success_at", "last_failure_at", "last_failure_category"):
        qualification[key] = previous.get(key)
    qualification["consecutive_failures"] = int(previous.get("consecutive_failures", 0))
    if previous.get("qualification_status") == "enabled" and previous.get("revalidation_due"):
        qualification["revalidation_due"] = True
        qualification["operational_status"] = previous.get("operational_status", "degraded")


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("timestamp must use canonical UTC Z notation")
    parsed = datetime.fromisoformat(f"{value[:-1]}+00:00")
    if not _is_utc_datetime(parsed):
        raise ValueError("timestamp must be UTC-aware")
    return parsed
