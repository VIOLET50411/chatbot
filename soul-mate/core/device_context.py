"""Owner 设备与健康汇总的独立、可撤销存储。"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from collections.abc import Iterator


DEFAULT_CONSENT = {
    "device_status": True,
    "app_activity": True,
    "city": True,
    "health_activity": True,
    "health_sleep": True,
    "health_heart": True,
}
CONSENT_KEYS = frozenset(DEFAULT_CONSENT)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _database_path() -> Path:
    configured = os.getenv("PLATFORM_DEVICE_DB", "data/platform/device_context.db")
    path = Path(configured).expanduser()
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[1] / path
    return path.resolve()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _bounded_text(value: Any, *, maximum: int) -> str:
    return str(value or "").strip()[:maximum]


def _number(value: Any, *, minimum: float, maximum: float) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return max(minimum, min(maximum, number))


def _integer(value: Any, *, minimum: int, maximum: int) -> int | None:
    number = _number(value, minimum=float(minimum), maximum=float(maximum))
    return int(round(number)) if number is not None else None


@dataclass(frozen=True, slots=True)
class IssuedDevice:
    device: dict[str, Any]
    token: str


class DeviceContextService:
    def __init__(self, database_path: str | Path | None = None) -> None:
        self.database_path = Path(database_path).resolve() if database_path else _database_path()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize_schema(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS owner_devices (
                    id TEXT PRIMARY KEY,
                    owner_user_id TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                    consent_json TEXT NOT NULL,
                    retention_days INTEGER NOT NULL DEFAULT 7 CHECK (retention_days BETWEEN 1 AND 90),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_seen_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_owner_devices_owner
                ON owner_devices(owner_user_id, enabled, updated_at DESC);

                CREATE TABLE IF NOT EXISTS owner_device_snapshots (
                    id TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL REFERENCES owner_devices(id) ON DELETE CASCADE,
                    captured_at TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    event_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    UNIQUE(device_id, event_hash)
                );
                CREATE INDEX IF NOT EXISTS idx_owner_device_snapshots_device_time
                ON owner_device_snapshots(device_id, captured_at DESC);

                CREATE TABLE IF NOT EXISTS owner_profile_facts (
                    owner_user_id TEXT NOT NULL,
                    fact_key TEXT NOT NULL,
                    fact_value TEXT NOT NULL,
                    source TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (owner_user_id, fact_key)
                );
                """
            )

    @staticmethod
    def _normalize_consent(value: dict[str, Any] | None) -> dict[str, bool]:
        source = value or {}
        return {key: bool(source.get(key, default)) for key, default in DEFAULT_CONSENT.items()}

    @staticmethod
    def _device_view(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": str(row["id"]),
            "owner_user_id": str(row["owner_user_id"]),
            "display_name": str(row["display_name"]),
            "enabled": bool(row["enabled"]),
            "consent": json.loads(str(row["consent_json"])),
            "retention_days": int(row["retention_days"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "last_seen_at": str(row["last_seen_at"] or ""),
        }

    def create_device(
        self,
        *,
        display_name: str,
        owner_user_id: str = "owner",
        consent: dict[str, Any] | None = None,
        retention_days: int = 7,
    ) -> IssuedDevice:
        normalized_name = _bounded_text(display_name, maximum=80)
        normalized_owner = _bounded_text(owner_user_id, maximum=80)
        if not normalized_name or not normalized_owner:
            raise ValueError("设备名称和数据主人不能为空")
        retention = max(1, min(90, int(retention_days)))
        token = secrets.token_urlsafe(36)
        device_id = str(uuid.uuid4())
        now = _iso(_utc_now())
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO owner_devices(
                    id, owner_user_id, display_name, token_hash, enabled,
                    consent_json, retention_days, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?)
                """,
                (
                    device_id,
                    normalized_owner,
                    normalized_name,
                    _token_hash(token),
                    json.dumps(self._normalize_consent(consent), ensure_ascii=False, sort_keys=True),
                    retention,
                    now,
                    now,
                ),
            )
            row = connection.execute("SELECT * FROM owner_devices WHERE id=?", (device_id,)).fetchone()
        if row is None:
            raise RuntimeError("设备创建失败")
        return IssuedDevice(device=self._device_view(row), token=token)

    def list_devices(self, *, owner_user_id: str = "owner") -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM owner_devices WHERE owner_user_id=? ORDER BY updated_at DESC",
                (owner_user_id,),
            ).fetchall()
        return [self._device_view(row) for row in rows]

    def set_owner_fact(
        self,
        *,
        fact_key: str,
        fact_value: str,
        owner_user_id: str = "owner",
        source: str = "owner_confirmed",
    ) -> None:
        normalized_owner = _bounded_text(owner_user_id, maximum=80)
        normalized_key = _bounded_text(fact_key, maximum=80)
        normalized_value = _bounded_text(fact_value, maximum=500)
        normalized_source = _bounded_text(source, maximum=80)
        if not all((normalized_owner, normalized_key, normalized_value, normalized_source)):
            raise ValueError("Owner 事实的主人、字段、内容和来源不能为空")
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO owner_profile_facts(owner_user_id, fact_key, fact_value, source, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(owner_user_id, fact_key) DO UPDATE SET
                    fact_value=excluded.fact_value,
                    source=excluded.source,
                    updated_at=excluded.updated_at
                """,
                (normalized_owner, normalized_key, normalized_value, normalized_source, _iso(_utc_now())),
            )

    def owner_profile(self, *, owner_user_id: str = "owner") -> dict[str, str]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT fact_key, fact_value FROM owner_profile_facts WHERE owner_user_id=? ORDER BY fact_key",
                (_bounded_text(owner_user_id, maximum=80),),
            ).fetchall()
        return {str(row["fact_key"]): str(row["fact_value"]) for row in rows}

    def update_device(
        self,
        device_id: str,
        *,
        display_name: str | None = None,
        enabled: bool | None = None,
        consent: dict[str, Any] | None = None,
        retention_days: int | None = None,
    ) -> dict[str, Any]:
        with self._connection() as connection:
            current = connection.execute("SELECT * FROM owner_devices WHERE id=?", (device_id,)).fetchone()
            if current is None:
                raise KeyError("设备不存在")
            next_name = _bounded_text(display_name, maximum=80) if display_name is not None else str(current["display_name"])
            if not next_name:
                raise ValueError("设备名称不能为空")
            next_consent = (
                self._normalize_consent(consent)
                if consent is not None
                else json.loads(str(current["consent_json"]))
            )
            retention = (
                max(1, min(90, int(retention_days)))
                if retention_days is not None
                else int(current["retention_days"])
            )
            connection.execute(
                """
                UPDATE owner_devices
                SET display_name=?, enabled=?, consent_json=?, retention_days=?, updated_at=?
                WHERE id=?
                """,
                (
                    next_name,
                    int(enabled if enabled is not None else bool(current["enabled"])),
                    json.dumps(next_consent, ensure_ascii=False, sort_keys=True),
                    retention,
                    _iso(_utc_now()),
                    device_id,
                ),
            )
            row = connection.execute("SELECT * FROM owner_devices WHERE id=?", (device_id,)).fetchone()
        return self._device_view(row)

    def rotate_token(self, device_id: str) -> str:
        token = secrets.token_urlsafe(36)
        with self._connection() as connection:
            result = connection.execute(
                "UPDATE owner_devices SET token_hash=?, updated_at=? WHERE id=?",
                (_token_hash(token), _iso(_utc_now()), device_id),
            )
            if result.rowcount != 1:
                raise KeyError("设备不存在")
        return token

    def _sanitize_snapshot(self, payload: dict[str, Any], consent: dict[str, bool]) -> dict[str, Any]:
        clean: dict[str, Any] = {}
        if consent["device_status"]:
            for key, value in (
                ("battery", _integer(payload.get("battery"), minimum=0, maximum=100)),
                ("charging", bool(payload.get("charging")) if payload.get("charging") is not None else None),
                ("low_power", bool(payload.get("low_power")) if payload.get("low_power") is not None else None),
            ):
                if value is not None:
                    clean[key] = value
            for key, maximum in (("device", 80), ("network", 32), ("focus_mode", 80), ("screen_state", 32)):
                value = _bounded_text(payload.get(key), maximum=maximum)
                if value:
                    clean[key] = value
        if consent["app_activity"]:
            app_name = _bounded_text(payload.get("app_name") or payload.get("app"), maximum=120)
            if app_name:
                clean["app_name"] = app_name
        if consent["city"]:
            city = _bounded_text(payload.get("city"), maximum=80)
            if city:
                clean["city"] = city
                accuracy = _number(payload.get("location_accuracy_m"), minimum=0, maximum=100_000)
                if accuracy is not None:
                    clean["location_accuracy_m"] = round(accuracy, 1)
        health = payload.get("health") if isinstance(payload.get("health"), dict) else payload
        if consent["health_activity"]:
            steps = _integer(health.get("steps"), minimum=0, maximum=200_000)
            energy = _number(health.get("active_energy_kcal"), minimum=0, maximum=20_000)
            if steps is not None:
                clean["steps"] = steps
            if energy is not None:
                clean["active_energy_kcal"] = round(energy, 1)
        if consent["health_sleep"]:
            sleep = _integer(health.get("sleep_minutes"), minimum=0, maximum=1_440)
            if sleep is not None:
                clean["sleep_minutes"] = sleep
        if consent["health_heart"]:
            average = _integer(health.get("heart_rate_avg"), minimum=20, maximum=260)
            maximum = _integer(health.get("heart_rate_max"), minimum=20, maximum=260)
            if average is not None:
                clean["heart_rate_avg"] = average
            if maximum is not None:
                clean["heart_rate_max"] = maximum
        return clean

    def ingest(self, *, token: str, payload: dict[str, Any]) -> dict[str, Any]:
        normalized_token = str(token or "").strip()
        if len(normalized_token) < 24:
            raise PermissionError("设备口令无效")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM owner_devices WHERE token_hash=? AND enabled=1",
                (_token_hash(normalized_token),),
            ).fetchone()
            if row is None:
                raise PermissionError("设备口令无效或设备已停用")
            consent = self._normalize_consent(json.loads(str(row["consent_json"])))
            clean = self._sanitize_snapshot(payload, consent)
            if not clean:
                raise ValueError("没有处于授权范围内的有效设备数据")
            captured_raw = str(payload.get("timestamp") or payload.get("captured_at") or "").strip()
            try:
                captured = datetime.fromisoformat(captured_raw.replace("Z", "+00:00")) if captured_raw else _utc_now()
            except ValueError as exc:
                raise ValueError("设备时间格式无效") from exc
            if captured.tzinfo is None or captured.utcoffset() is None:
                raise ValueError("设备时间必须包含时区")
            captured = captured.astimezone(timezone.utc)
            now = _utc_now()
            if captured < now - timedelta(days=2) or captured > now + timedelta(minutes=15):
                raise ValueError("设备时间超出允许范围")
            material = json.dumps(
                {"captured_at": _iso(captured), "payload": clean},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            event_hash = hashlib.sha256(material.encode("utf-8")).hexdigest()
            snapshot_id = str(uuid.uuid4())
            result = connection.execute(
                """
                INSERT OR IGNORE INTO owner_device_snapshots(
                    id, device_id, captured_at, received_at, event_hash, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (snapshot_id, str(row["id"]), _iso(captured), _iso(now), event_hash, json.dumps(clean, ensure_ascii=False, sort_keys=True)),
            )
            connection.execute(
                "UPDATE owner_devices SET last_seen_at=?, updated_at=? WHERE id=?",
                (_iso(now), _iso(now), str(row["id"])),
            )
        self.purge_expired()
        return {"status": "accepted" if result.rowcount == 1 else "duplicate", "captured_at": _iso(captured)}

    def purge_expired(self) -> int:
        with self._connection() as connection:
            result = connection.execute(
                """
                DELETE FROM owner_device_snapshots
                WHERE EXISTS (
                    SELECT 1 FROM owner_devices d
                    WHERE d.id=owner_device_snapshots.device_id
                      AND julianday(owner_device_snapshots.captured_at)
                          < julianday('now', '-' || d.retention_days || ' days')
                )
                """
            )
        return int(result.rowcount)

    def status(self, *, owner_user_id: str = "owner", limit: int = 24) -> dict[str, Any]:
        devices = self.list_devices(owner_user_id=owner_user_id)
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT s.*, d.display_name
                FROM owner_device_snapshots s
                JOIN owner_devices d ON d.id=s.device_id
                WHERE d.owner_user_id=? AND d.enabled=1
                ORDER BY s.captured_at DESC LIMIT ?
                """,
                (owner_user_id, max(1, min(200, int(limit)))),
            ).fetchall()
        records = [
            {
                "id": str(row["id"]),
                "device_id": str(row["device_id"]),
                "device_name": str(row["display_name"]),
                "captured_at": str(row["captured_at"]),
                "received_at": str(row["received_at"]),
                "data": json.loads(str(row["payload_json"])),
            }
            for row in rows
        ]
        return {"devices": devices, "records": records, "record_count": len(records)}

    def prompt_context(self, *, owner_user_id: str = "owner") -> str:
        profile = self.owner_profile(owner_user_id=owner_user_id)
        identity_parts: list[str] = []
        if profile.get("birth_date"):
            identity_parts.append(f"出生日期 {profile['birth_date']}")
        identity_context = (
            "## Owner 已亲自确认的资料（不是人物自身设定）\n"
            + "，".join(identity_parts)
            if identity_parts
            else ""
        )
        current = self.status(owner_user_id=owner_user_id, limit=8)
        records = current["records"]
        if not records:
            return identity_context
        latest = records[0]
        try:
            captured = datetime.fromisoformat(latest["captured_at"])
        except ValueError:
            return identity_context
        age_minutes = max(0, int((_utc_now() - captured.astimezone(timezone.utc)).total_seconds() // 60))
        if age_minutes > 24 * 60:
            return identity_context
        data = latest["data"]
        parts: list[str] = []
        if "battery" in data:
            parts.append(f"电量 {data['battery']}%" + (" 正在充电" if data.get("charging") else ""))
        if data.get("focus_mode"):
            parts.append(f"专注模式 {data['focus_mode']}")
        if data.get("app_name"):
            parts.append(f"最近打开 {data['app_name']}")
        if data.get("city"):
            parts.append(f"城市 {data['city']}")
        if "steps" in data:
            parts.append(f"今日 {data['steps']} 步")
        if "sleep_minutes" in data:
            parts.append(f"睡眠约 {round(data['sleep_minutes'] / 60, 1)} 小时")
        if "heart_rate_avg" in data:
            parts.append(f"平均心率 {data['heart_rate_avg']} bpm")
        if not parts:
            return identity_context
        freshness = "刚刚" if age_minutes < 10 else f"约 {age_minutes} 分钟前"
        device_context = (
            "## Owner 明确授权的设备状态（只用于自然关心，不是人物事实）\n"
            + freshness
            + "："
            + "，".join(parts)
            + "。这些是设备汇总，可能延迟或不完整；不要诊断疾病、监视、指责或假装知道未上传的信息。"
        )
        return "\n\n".join(filter(None, (identity_context, device_context)))


_service: DeviceContextService | None = None


def get_device_context_service() -> DeviceContextService:
    global _service
    if _service is None or _service.database_path != _database_path():
        _service = DeviceContextService()
    return _service
