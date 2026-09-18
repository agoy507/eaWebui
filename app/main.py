from __future__ import annotations

import asyncio
import copy
import json
import os
import secrets
from contextlib import asynccontextmanager, suppress
from datetime import datetime, time, timedelta, timezone
from ipaddress import ip_address, ip_network
from pathlib import Path
from threading import RLock
from time import monotonic
from typing import Callable, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.security import (
    SESSION_TTL_SECONDS,
    create_api_token,
    create_session_token,
    decode_session_token,
    hash_api_token,
    hash_password,
    verify_password,
)


BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "app" / "static"
DEFAULT_STATE_FILE = Path(
    os.getenv("EA_CONTROL_STATE_FILE", str(BASE_DIR / "data" / "state.json"))
).resolve()
DEFAULT_EA_ID = "rsi-martingale-xauusd-m1"
SESSION_COOKIE = "ea_control_session"
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "0").strip().lower() in {"1", "true", "yes"}
SCHEDULE_TIMEZONE_NAME = "Asia/Jakarta"
SCHEDULE_TIMEZONE = timezone(timedelta(hours=7), name=SCHEDULE_TIMEZONE_NAME)
TIME_PATTERN = r"^(?:[01]\d|2[0-3]):[0-5]\d$"
USERNAME_PATTERN = r"^[a-z0-9_.-]{3,32}$"
EA_ID_PATTERN = r"^[A-Za-z0-9_.-]{3,100}$"
EA_CLIENT_LEASE_SECONDS = 8.0
STRATEGY_STANDARD = "standard"
STRATEGY_TRAILING = "trailing"
STRATEGY_TYPES = {STRATEGY_STANDARD, STRATEGY_TRAILING}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalized_utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=SCHEDULE_TIMEZONE)
    return value.astimezone(timezone.utc)


def parse_saved_datetime(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return normalized_utc_datetime(datetime.fromisoformat(str(value)))
    except (TypeError, ValueError):
        return None


def normalize_ip_rule(value: str) -> str:
    value = value.strip()
    if value == "*":
        return value
    try:
        if "/" in value:
            return str(ip_network(value, strict=False))
        return str(ip_address(value))
    except ValueError as exc:
        raise ValueError("IP harus berupa alamat IP, CIDR, atau *") from exc


def ip_is_allowed(source_ip: str, rule: str) -> bool:
    if rule == "*":
        return True
    try:
        source = ip_address(source_ip)
        if source.version == 6 and source.ipv4_mapped:
            source = source.ipv4_mapped
        if "/" in rule:
            return source in ip_network(rule, strict=False)
        return source == ip_address(rule)
    except ValueError:
        return False


class EaConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rsi_period: int = Field(default=5, ge=2, le=200)
    rsi_buy_level: int = Field(default=15, ge=1, le=49)
    rsi_sell_level: int = Field(default=95, ge=51, le=100)
    initial_lot: float = Field(default=0.01, gt=0, le=100)
    sl_pips_from_last: int = Field(default=20, ge=1, le=100_000)
    tp_pips_from_bep: int = Field(default=20, ge=1, le=100_000)
    martingale_enabled: bool = True
    martingale_distance_pips: int = Field(default=15, ge=1, le=100_000)
    lot_multiplier: float = Field(default=1.5, ge=1, le=10)
    max_martingale: int = Field(default=5, ge=0)
    max_lot: float = Field(default=0.5, gt=0, le=100)

    @model_validator(mode="after")
    def validate_relationships(self) -> "EaConfig":
        if self.rsi_buy_level >= self.rsi_sell_level:
            raise ValueError("Level RSI beli harus lebih kecil dari level RSI jual")
        if self.max_lot < self.initial_lot:
            raise ValueError("Lot maksimal tidak boleh lebih kecil dari lot awal")
        return self


class TrailingEaConfig(BaseModel):
    """Runtime configuration for the RSI martingale with group trailing SL."""

    model_config = ConfigDict(extra="forbid")

    rsi_period: int = Field(default=5, ge=2, le=200)
    rsi_buy_level: int = Field(default=15, ge=1, le=49)
    rsi_sell_level: int = Field(default=95, ge=51, le=100)
    initial_lot: float = Field(default=0.01, gt=0, le=100)
    initial_sl_pips: int = Field(default=300, ge=1, le=100_000)
    first_layer_tp_pips: int = Field(default=20, ge=1, le=100_000)
    trailing_start_pips: int = Field(default=10, ge=1, le=100_000)
    trailing_distance_pips: int = Field(default=5, ge=1, le=100_000)
    trailing_step_pips: int = Field(default=3, ge=1, le=100_000)
    martingale_enabled: bool = True
    martingale_distance_pips: int = Field(default=30, ge=1, le=100_000)
    lot_multiplier: float = Field(default=1.5, ge=1, le=10)
    max_martingale: int = Field(default=10, ge=0)
    max_lot: float = Field(default=1.0, gt=0, le=100)

    @model_validator(mode="after")
    def validate_relationships(self) -> "TrailingEaConfig":
        if self.rsi_buy_level >= self.rsi_sell_level:
            raise ValueError("Level RSI beli harus lebih kecil dari level RSI jual")
        if self.max_lot < self.initial_lot:
            raise ValueError("Lot maksimal tidak boleh lebih kecil dari lot awal")
        if self.trailing_distance_pips > self.trailing_start_pips:
            raise ValueError("Jarak trailing tidak boleh lebih besar dari mulai trailing")
        return self


def config_model_for_strategy(strategy_type: str) -> type[EaConfig] | type[TrailingEaConfig]:
    return TrailingEaConfig if strategy_type == STRATEGY_TRAILING else EaConfig


def default_config_for_strategy(strategy_type: str) -> dict:
    return config_model_for_strategy(strategy_type)().model_dump()


class ConfigUpdate(EaConfig):
    refresh_protection: bool = False


class UserConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    initial_lot: float = Field(gt=0, le=100)
    sl_pips_from_last: int = Field(ge=1, le=100_000)
    tp_pips_from_bep: int = Field(ge=1, le=100_000)
    martingale_enabled: bool
    martingale_distance_pips: int = Field(ge=1, le=100_000)
    lot_multiplier: float = Field(ge=1, le=10)
    max_martingale: int = Field(ge=0)
    max_lot: float = Field(gt=0, le=100)
    refresh_protection: bool = False

    @model_validator(mode="after")
    def validate_lots(self) -> "UserConfigUpdate":
        if self.max_lot < self.initial_lot:
            raise ValueError("Lot maksimal tidak boleh lebih kecil dari lot awal")
        return self


class EaStrategyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy_type: Literal["standard", "trailing"]


class ControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["start", "stop"]
    stop_mode: Literal["pause_only", "delete_pending", "close_all"] = "delete_pending"


class ScheduleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    start_time: str = Field(default="06:00", pattern=TIME_PATTERN)
    stop_time: str = Field(default="23:00", pattern=TIME_PATTERN)

    @model_validator(mode="after")
    def validate_times(self) -> "ScheduleConfig":
        if self.start_time == self.stop_time:
            raise ValueError("Waktu START dan STOP tidak boleh sama")
        return self


class EaStatus(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ea_id: str = Field(min_length=3, max_length=100, pattern=EA_ID_PATTERN)
    client_id: str = Field(min_length=3, max_length=100, pattern=EA_ID_PATTERN)
    ea_version: str = Field(default="", max_length=20)
    strategy_type: Literal["standard", "trailing"] = STRATEGY_STANDARD
    symbol: str = Field(min_length=1, max_length=40)
    account_login: str = Field(min_length=1, max_length=40)
    algo_enabled: bool
    bid: float = 0
    ask: float = 0
    rsi: float = 0
    buy_positions: int = Field(default=0, ge=0)
    sell_positions: int = Field(default=0, ge=0)
    total_positions: int = Field(default=0, ge=0)
    floating_profit: float = 0
    account_floating_profit: float | None = None
    account_balance: float | None = None
    account_currency: str = Field(default="", max_length=12)
    daily_profit: float | None = None
    daily_profit_date: str = Field(default="", max_length=10)
    account_daily_profit: float | None = None
    account_profit_date: str = Field(default="", max_length=10)
    config_revision: int = Field(default=0, ge=0)
    last_error: str = Field(default="", max_length=500)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=3, max_length=32, pattern=USERNAME_PATTERN)
    password: str = Field(min_length=8, max_length=128)

    @field_validator("username", mode="before")
    @classmethod
    def normalize_username(cls, value: str) -> str:
        return str(value).strip().lower()


class PasswordChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: str = Field(min_length=8, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)

    @model_validator(mode="after")
    def passwords_must_differ(self) -> "PasswordChangeRequest":
        if self.current_password == self.new_password:
            raise ValueError("Password baru harus berbeda dari password lama")
        return self


class PasswordVerificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: str = Field(min_length=8, max_length=128)


class AccountCreate(LoginRequest):
    ea_id: str = Field(default=DEFAULT_EA_ID, min_length=3, max_length=100, pattern=EA_ID_PATTERN)
    allowed_ip: str = Field(default="*", min_length=1, max_length=64)
    expires_at: datetime | None = None

    @field_validator("ea_id", mode="before")
    @classmethod
    def normalize_ea_id(cls, value: str) -> str:
        return str(value).strip()

    @field_validator("allowed_ip", mode="before")
    @classmethod
    def validate_allowed_ip(cls, value: str) -> str:
        return normalize_ip_rule(str(value))


class UserUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed_ip: str | None = Field(default=None, min_length=1, max_length=64)
    enabled: bool | None = None
    password: str | None = Field(default=None, min_length=8, max_length=128)

    @field_validator("allowed_ip", mode="before")
    @classmethod
    def validate_allowed_ip(cls, value: str | None) -> str | None:
        return None if value is None else normalize_ip_rule(str(value))


class UserExpirationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expires_at: datetime


class EaCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ea_id: str = Field(min_length=3, max_length=100, pattern=EA_ID_PATTERN)
    allowed_ip: str = Field(default="*", min_length=1, max_length=64)

    @field_validator("ea_id", mode="before")
    @classmethod
    def normalize_ea_id(cls, value: str) -> str:
        return str(value).strip()

    @field_validator("allowed_ip", mode="before")
    @classmethod
    def validate_allowed_ip(cls, value: str) -> str:
        return normalize_ip_rule(str(value))


class EaUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed_ip: str | None = Field(default=None, min_length=1, max_length=64)
    daily_profit_auto_stop_enabled: bool | None = None
    daily_profit_target: float | None = Field(default=None, ge=0, le=10_000_000)

    @field_validator("allowed_ip", mode="before")
    @classmethod
    def validate_allowed_ip(cls, value: str | None) -> str | None:
        return None if value is None else normalize_ip_rule(str(value))

    @model_validator(mode="after")
    def validate_profit_target(self) -> "EaUpdateRequest":
        if self.daily_profit_auto_stop_enabled is True and self.daily_profit_target is not None and self.daily_profit_target <= 0:
            raise ValueError("Target profit harian harus lebih besar dari 0 saat auto-stop aktif")
        return self


class DailyProfitAutoStopUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    target: float | None = Field(default=None, ge=0, le=10_000_000)

    @model_validator(mode="after")
    def validate_target(self) -> "DailyProfitAutoStopUpdate":
        if self.enabled and (self.target is None or self.target <= 0):
            raise ValueError("Target profit harian harus lebih besar dari 0 saat fitur aktif")
        return self


def normalize_trading_mode_name(value: str) -> str:
    normalized = " ".join(str(value).strip().split())
    if not normalized:
        raise ValueError("Nama mode trading wajib diisi")
    if len(normalized) > 40:
        raise ValueError("Nama mode trading maksimal 40 karakter")
    if any(ord(character) < 32 for character in normalized):
        raise ValueError("Nama mode trading mengandung karakter yang tidak valid")
    return normalized


class TradingModeSaveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=40)
    rsi_period: int = Field(ge=2, le=200)
    rsi_buy_level: int = Field(ge=1, le=49)
    rsi_sell_level: int = Field(ge=51, le=100)

    @field_validator("name", mode="before")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return normalize_trading_mode_name(value)

    @model_validator(mode="after")
    def validate_rsi_levels(self) -> "TradingModeSaveRequest":
        if self.rsi_buy_level >= self.rsi_sell_level:
            raise ValueError("Level RSI beli harus lebih kecil dari level RSI jual")
        return self


class TradingModeSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=40)

    @field_validator("name", mode="before")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return normalize_trading_mode_name(value)


def normalize_trading_mode_catalog(saved_modes: object, fallback_updated_at: str) -> dict[str, dict]:
    normalized_modes: dict[str, dict] = {}
    if not isinstance(saved_modes, dict):
        return normalized_modes
    for mode in saved_modes.values():
        if not isinstance(mode, dict):
            continue
        try:
            validated_mode = TradingModeSaveRequest.model_validate(
                {
                    field: mode.get(field)
                    for field in ("name", "rsi_period", "rsi_buy_level", "rsi_sell_level")
                }
            )
        except (TypeError, ValueError):
            continue
        mode_data = validated_mode.model_dump()
        mode_data["updated_at"] = str(mode.get("updated_at") or fallback_updated_at)
        normalized_modes[validated_mode.name.casefold()] = mode_data
    return normalized_modes


def same_trading_mode_parameters(first: dict, second: dict) -> bool:
    return all(
        first.get(field) == second.get(field)
        for field in ("rsi_period", "rsi_buy_level", "rsi_sell_level")
    )


def default_instance(
    ea_id: str,
    owner: str | None,
    allowed_ip: str,
    strategy_type: str = STRATEGY_STANDARD,
) -> dict:
    normalized_strategy = strategy_type if strategy_type in STRATEGY_TYPES else STRATEGY_STANDARD
    default_config = default_config_for_strategy(normalized_strategy)
    return {
        "ea_id": ea_id,
        "owner": owner,
        "allowed_ip": allowed_ip,
        "api_token_hash": "",
        "api_token_hint": "",
        "strategy_type": normalized_strategy,
        "strategy_configs": {normalized_strategy: copy.deepcopy(default_config)},
        "config": default_config,
        "active_trading_mode": None,
        # Dipertahankan hanya agar upgrade tidak menghapus data preset versi lama.
        "presets": {},
        "config_revision": 1,
        "command_revision": 1,
        "refresh_protection_revision": 0,
        "algo_enabled": True,
        "stop_mode": "delete_pending",
        "daily_profit_auto_stop_enabled": False,
        "daily_profit_target": 0.0,
        "daily_profit_auto_stop_date": "",
        "daily_profit_auto_stop_triggered": False,
        "schedule": ScheduleConfig().model_dump(),
        "schedule_phase": "disabled",
        "schedule_last_action": None,
        "schedule_last_action_at": None,
        "updated_at": utc_now(),
        "status": None,
        "lease": None,
    }


def default_root_state() -> dict:
    return {
        "schema_version": 2,
        "auth_secret": secrets.token_urlsafe(48),
        "users": {},
        "trading_modes": {},
        "instances": {},
        "updated_at": utc_now(),
    }


class StateStore:
    def __init__(self, path: Path, now_provider: Callable[[], datetime] | None = None):
        self.path = path
        self.lock = RLock()
        self.now_provider = now_provider or (lambda: datetime.now(SCHEDULE_TIMEZONE))
        self.login_attempts: dict[str, list[float]] = {}
        self.state = self._load()
        with self.lock:
            self._expire_due_users_locked()

    def _load(self) -> dict:
        if not self.path.exists():
            return default_root_state()
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if loaded.get("schema_version") != 2:
                return self._migrate_legacy(loaded)
            root = default_root_state()
            root.update(loaded)
            root["users"] = dict(root.get("users") or {})
            normalized_global_modes = normalize_trading_mode_catalog(
                root.get("trading_modes"),
                str(root.get("updated_at") or utc_now()),
            )
            normalized_instances: dict[str, dict] = {}
            for ea_id, saved in dict(root.get("instances") or {}).items():
                saved_strategy = str(saved.get("strategy_type") or STRATEGY_STANDARD).strip().lower()
                strategy_type = saved_strategy if saved_strategy in STRATEGY_TYPES else STRATEGY_STANDARD
                base = default_instance(
                    ea_id,
                    saved.get("owner"),
                    saved.get("allowed_ip", "*"),
                    strategy_type,
                )
                base.update(saved)
                base["strategy_type"] = strategy_type
                saved_configs = base.get("strategy_configs")
                saved_configs = saved_configs if isinstance(saved_configs, dict) else {}
                normalized_configs: dict[str, dict] = {}
                for candidate_strategy in STRATEGY_TYPES:
                    # ``config`` is the persisted source of truth for the
                    # active EA.  strategy_configs is only a cache for the
                    # inactive type, so it must never overwrite a newer
                    # active configuration during an upgrade/restart.
                    candidate_config = (
                        saved.get("config")
                        if candidate_strategy == strategy_type and isinstance(saved.get("config"), dict)
                        else saved_configs.get(candidate_strategy)
                    )
                    try:
                        normalized_configs[candidate_strategy] = config_model_for_strategy(
                            candidate_strategy
                        ).model_validate(candidate_config or default_config_for_strategy(candidate_strategy)).model_dump()
                    except (TypeError, ValueError):
                        normalized_configs[candidate_strategy] = default_config_for_strategy(candidate_strategy)
                base["strategy_configs"] = normalized_configs
                base["config"] = copy.deepcopy(normalized_configs[strategy_type])
                base["schedule"] = ScheduleConfig.model_validate(base["schedule"]).model_dump()
                base["allowed_ip"] = normalize_ip_rule(base["allowed_ip"])
                saved_presets = base.get("presets")
                base["presets"] = saved_presets if isinstance(saved_presets, dict) else {}
                # V2.5.3 sebelumnya menyimpan Entry Mode per instance. Profil valid
                # digabung ke katalog global dan salinannya disimpan untuk rollback.
                legacy_modes = normalize_trading_mode_catalog(saved.get("trading_modes"), base["updated_at"])
                legacy_mode_keys: dict[str, str] = {}
                if legacy_modes:
                    base["legacy_trading_modes"] = copy.deepcopy(legacy_modes)
                    for mode_key, mode in legacy_modes.items():
                        global_key = mode_key
                        existing_mode = normalized_global_modes.get(global_key)
                        if existing_mode and not same_trading_mode_parameters(existing_mode, mode):
                            owner_label = str(base.get("owner") or ea_id).strip()[:20] or "EA"
                            counter = 1
                            while True:
                                suffix = f" ({owner_label}{f'-{counter}' if counter > 1 else ''})"
                                candidate_name = f"{mode['name'][: 40 - len(suffix)].rstrip()}{suffix}"
                                global_key = candidate_name.casefold()
                                candidate = normalized_global_modes.get(global_key)
                                if not candidate or same_trading_mode_parameters(candidate, mode):
                                    mode = {**mode, "name": candidate_name}
                                    break
                                counter += 1
                        normalized_global_modes.setdefault(global_key, mode)
                        legacy_mode_keys[mode_key] = global_key
                base.pop("trading_modes", None)
                active_mode = str(base.get("active_trading_mode") or "").casefold()
                base["active_trading_mode"] = legacy_mode_keys.get(active_mode, active_mode) or None
                base["status"] = None
                base["lease"] = None
                normalized_instances[ea_id] = base
            root["trading_modes"] = normalized_global_modes
            for instance in normalized_instances.values():
                active_mode = instance.get("active_trading_mode")
                if active_mode not in normalized_global_modes:
                    instance["active_trading_mode"] = None
            root["instances"] = normalized_instances
            for username, user in root["users"].items():
                if user.get("role") == "admin":
                    user["expires_at"] = None
                    user["expired_at"] = None
                    user["disabled_reason"] = None
                else:
                    saved_expiration = parse_saved_datetime(user.get("expires_at"))
                    user["expires_at"] = saved_expiration.isoformat() if saved_expiration else None
                    saved_expired_at = parse_saved_datetime(user.get("expired_at"))
                    user["expired_at"] = saved_expired_at.isoformat() if saved_expired_at else None
                    reason = user.get("disabled_reason")
                    user["disabled_reason"] = reason if reason in {"manual", "expired"} else None
                requested_ids = user.get("ea_ids")
                if not isinstance(requested_ids, list):
                    requested_ids = []
                primary_ea_id = str(user.get("ea_id") or "").strip()
                candidates = [primary_ea_id, *requested_ids]
                candidates.extend(
                    ea_id
                    for ea_id, instance in normalized_instances.items()
                    if instance.get("owner") == username
                )
                ea_ids: list[str] = []
                for ea_id in candidates:
                    if (
                        ea_id
                        and ea_id not in ea_ids
                        and ea_id in normalized_instances
                        and normalized_instances[ea_id].get("owner") == username
                    ):
                        ea_ids.append(ea_id)
                user["ea_ids"] = ea_ids
                user["ea_id"] = ea_ids[0] if ea_ids else primary_ea_id
            return root
        except (OSError, json.JSONDecodeError, ValueError):
            broken_path = self.path.with_suffix(f".broken-{int(datetime.now().timestamp())}.json")
            try:
                self.path.replace(broken_path)
            except OSError:
                pass
            return default_root_state()

    def _migrate_legacy(self, loaded: dict) -> dict:
        root = default_root_state()
        instance = default_instance(DEFAULT_EA_ID, None, "*")
        for field in (
            "config",
            "config_revision",
            "command_revision",
            "refresh_protection_revision",
            "algo_enabled",
            "stop_mode",
            "schedule",
            "schedule_phase",
            "schedule_last_action",
            "schedule_last_action_at",
            "updated_at",
        ):
            if field in loaded:
                instance[field] = loaded[field]
        instance["config"] = EaConfig.model_validate(instance["config"]).model_dump()
        instance["schedule"] = ScheduleConfig.model_validate(instance["schedule"]).model_dump()
        root["instances"][DEFAULT_EA_ID] = instance
        return root

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(".json.tmp")
        persisted = copy.deepcopy(self.state)
        for instance in persisted["instances"].values():
            instance["status"] = None
            instance["lease"] = None
        temp_path.write_text(json.dumps(persisted, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp_path, self.path)

    def _now_utc(self) -> datetime:
        current = self.now_provider()
        if current.tzinfo is None:
            current = current.replace(tzinfo=SCHEDULE_TIMEZONE)
        return current.astimezone(timezone.utc)

    def _user_expired_locked(self, user: dict, current: datetime | None = None) -> bool:
        if user.get("role") == "admin":
            return False
        expires_at = parse_saved_datetime(user.get("expires_at"))
        return bool(expires_at and expires_at <= (current or self._now_utc()))

    def _mark_user_expired_locked(self, user: dict, current: datetime) -> bool:
        if user.get("role") == "admin" or not user.get("enabled"):
            return False
        user["enabled"] = False
        user["disabled_reason"] = "expired"
        user["expired_at"] = current.isoformat()
        user["session_version"] = int(user.get("session_version", 1)) + 1
        updated_at = current.isoformat()
        for ea_id in self._user_ea_ids_locked(user):
            instance = self.state["instances"].get(ea_id)
            if not instance:
                continue
            if instance.get("algo_enabled"):
                instance["algo_enabled"] = False
                instance["command_revision"] += 1
            instance["status"] = None
            instance["lease"] = None
            instance["updated_at"] = updated_at
        self.state["updated_at"] = updated_at
        return True

    def _expire_due_users_locked(self) -> int:
        current = self._now_utc()
        expired_count = 0
        for user in self.state["users"].values():
            if self._user_expired_locked(user, current) and self._mark_user_expired_locked(user, current):
                expired_count += 1
        if expired_count:
            self._save()
        return expired_count

    def expire_due_users(self) -> int:
        with self.lock:
            return self._expire_due_users_locked()

    def setup_required(self) -> bool:
        with self.lock:
            return not bool(self.state["users"])

    @staticmethod
    def _user_ea_ids_locked(user: dict) -> list[str]:
        ea_ids = user.get("ea_ids")
        if not isinstance(ea_ids, list):
            ea_ids = []
        primary = str(user.get("ea_id") or "")
        return list(dict.fromkeys([ea_id for ea_id in [primary, *ea_ids] if ea_id]))

    def _public_ea_locked(self, ea_id: str) -> dict | None:
        instance = self.state["instances"].get(ea_id)
        if not instance:
            return None
        return {
            "ea_id": ea_id,
            "strategy_type": instance.get("strategy_type", STRATEGY_STANDARD),
            "allowed_ip": instance["allowed_ip"],
            "api_token_hint": instance["api_token_hint"],
            "api_token_active": bool(instance.get("api_token_hash")),
            "daily_profit_auto_stop_enabled": bool(instance.get("daily_profit_auto_stop_enabled", False)),
            "daily_profit_target": float(instance.get("daily_profit_target") or 0.0),
            "daily_profit_auto_stop_triggered": bool(instance.get("daily_profit_auto_stop_triggered", False)),
        }

    def _public_user_locked(self, user: dict) -> dict:
        ea_ids = self._user_ea_ids_locked(user)
        eas = [self._public_ea_locked(ea_id) for ea_id in ea_ids]
        eas = [ea for ea in eas if ea is not None]
        primary = eas[0] if eas else None
        return {
            "username": user["username"],
            "role": user["role"],
            "enabled": user["enabled"],
            "expires_at": user.get("expires_at"),
            "expired_at": user.get("expired_at"),
            "expired": self._user_expired_locked(user),
            "expiration_required": user["role"] != "admin" and not bool(user.get("expires_at")),
            "disabled_reason": user.get("disabled_reason"),
            "ea_id": primary["ea_id"] if primary else user.get("ea_id"),
            "ea_ids": [ea["ea_id"] for ea in eas],
            "eas": eas,
            "allowed_ip": primary["allowed_ip"] if primary else None,
            "api_token_hint": primary["api_token_hint"] if primary else None,
            "api_token_active": primary["api_token_active"] if primary else False,
        }

    def setup_admin(self, request: AccountCreate) -> tuple[dict, str, str]:
        with self.lock:
            if self.state["users"]:
                raise HTTPException(status_code=409, detail="Administrator awal sudah dibuat")
            token = create_api_token()
            instance = self.state["instances"].get(request.ea_id)
            if instance is None:
                instance = default_instance(request.ea_id, request.username, request.allowed_ip)
                self.state["instances"][request.ea_id] = instance
            elif instance.get("owner"):
                raise HTTPException(status_code=409, detail="EA ID sudah digunakan")
            instance.update(
                owner=request.username,
                allowed_ip=request.allowed_ip,
                api_token_hash=hash_api_token(token),
                api_token_hint=token[-6:],
            )
            user = {
                "username": request.username,
                "password_hash": hash_password(request.password),
                "role": "admin",
                "enabled": True,
                "session_version": 1,
                "ea_id": request.ea_id,
                "ea_ids": [request.ea_id],
                "expires_at": None,
                "expired_at": None,
                "disabled_reason": None,
                "created_at": utc_now(),
            }
            self.state["users"][request.username] = user
            self.state["updated_at"] = utc_now()
            self._save()
            session = create_session_token(self.state["auth_secret"], request.username, 1)
            return self._public_user_locked(user), session, token

    def login(self, request: LoginRequest, source_ip: str) -> tuple[dict, str]:
        with self.lock:
            self._expire_due_users_locked()
            now = monotonic()
            attempts = [attempt for attempt in self.login_attempts.get(source_ip, []) if now - attempt < 300]
            if len(attempts) >= 5:
                raise HTTPException(status_code=429, detail="Terlalu banyak percobaan login. Coba lagi dalam 5 menit")
            user = self.state["users"].get(request.username)
            if not user or not user["enabled"] or not verify_password(request.password, user["password_hash"]):
                attempts.append(now)
                self.login_attempts[source_ip] = attempts
                raise HTTPException(status_code=401, detail="Username atau password salah")
            self.login_attempts.pop(source_ip, None)
            session = create_session_token(
                self.state["auth_secret"],
                user["username"],
                user["session_version"],
            )
            return self._public_user_locked(user), session

    def session_user(self, token: str | None) -> dict | None:
        if not token:
            return None
        with self.lock:
            self._expire_due_users_locked()
            payload = decode_session_token(self.state["auth_secret"], token)
            if not payload:
                return None
            user = self.state["users"].get(str(payload.get("sub", "")))
            if not user or not user["enabled"] or user["session_version"] != payload.get("ver"):
                return None
            return self._public_user_locked(user)

    def create_user(self, request: AccountCreate) -> tuple[dict, str]:
        with self.lock:
            if request.username in self.state["users"]:
                raise HTTPException(status_code=409, detail="Username sudah digunakan")
            if request.ea_id in self.state["instances"]:
                raise HTTPException(status_code=409, detail="EA ID sudah digunakan")
            if request.expires_at is None:
                raise HTTPException(status_code=422, detail="Masa berlaku user wajib diisi")
            expires_at = normalized_utc_datetime(request.expires_at)
            if expires_at <= self._now_utc():
                raise HTTPException(status_code=422, detail="Masa berlaku harus berada di masa depan")
            token = create_api_token()
            instance = default_instance(request.ea_id, request.username, request.allowed_ip)
            instance["api_token_hash"] = hash_api_token(token)
            instance["api_token_hint"] = token[-6:]
            user = {
                "username": request.username,
                "password_hash": hash_password(request.password),
                "role": "user",
                "enabled": True,
                "session_version": 1,
                "ea_id": request.ea_id,
                "ea_ids": [request.ea_id],
                "expires_at": expires_at.isoformat(),
                "expired_at": None,
                "disabled_reason": None,
                "created_at": utc_now(),
            }
            self.state["users"][request.username] = user
            self.state["instances"][request.ea_id] = instance
            self.state["updated_at"] = utc_now()
            self._save()
            return self._public_user_locked(user), token

    def list_users(self) -> list[dict]:
        with self.lock:
            self._expire_due_users_locked()
            rows = []
            for username in sorted(self.state["users"]):
                user = self.state["users"][username]
                item = self._public_user_locked(user)
                enriched_eas = []
                for ea in item["eas"]:
                    instance = self.state["instances"].get(ea["ea_id"])
                    status = instance.get("status") if instance else None
                    online = self._is_online(status)
                    enriched = {
                        **ea,
                        "online": online,
                        "algorithm_status": (
                            "running" if online and bool(status.get("algo_enabled"))
                            else "stopped" if online
                            else "offline"
                        ),
                        "connection": (
                            {
                                "account_login": status.get("account_login", ""),
                                "symbol": status.get("symbol", ""),
                                "source_ip": status.get("source_ip", ""),
                                "last_seen": status.get("last_seen"),
                                "client_id": status.get("client_id", ""),
                                "ea_version": status.get("ea_version", ""),
                            }
                            if status
                            else None
                        ),
                        "financial": (
                            {
                                "account_balance": status.get("account_balance"),
                                "account_daily_profit": status.get("account_daily_profit"),
                                "account_currency": status.get("account_currency", ""),
                                "account_profit_date": status.get("account_profit_date", ""),
                            }
                            if status
                            else None
                        ),
                    }
                    enriched_eas.append(enriched)
                item["eas"] = enriched_eas
                primary = enriched_eas[0] if enriched_eas else {}
                for field in ("online", "algorithm_status", "connection", "financial"):
                    item[field] = primary.get(field)
                rows.append(item)
            return rows

    def add_ea_to_user(self, username: str, request: EaCreateRequest) -> tuple[dict, str]:
        username = username.strip().lower()
        with self.lock:
            user = self.state["users"].get(username)
            if not user:
                raise HTTPException(status_code=404, detail="User tidak ditemukan")
            if request.ea_id in self.state["instances"]:
                raise HTTPException(status_code=409, detail="EA ID sudah digunakan")
            token = create_api_token()
            instance = default_instance(request.ea_id, username, request.allowed_ip)
            instance["api_token_hash"] = hash_api_token(token)
            instance["api_token_hint"] = token[-6:]
            ea_ids = self._user_ea_ids_locked(user)
            ea_ids.append(request.ea_id)
            user["ea_ids"] = list(dict.fromkeys(ea_ids))
            if not user.get("ea_id"):
                user["ea_id"] = request.ea_id
            self.state["instances"][request.ea_id] = instance
            self.state["updated_at"] = utc_now()
            self._save()
            return self._public_user_locked(user), token

    def update_user_ea(self, username: str, ea_id: str, update: EaUpdateRequest) -> dict:
        username = username.strip().lower()
        with self.lock:
            user = self.state["users"].get(username)
            if not user:
                raise HTTPException(status_code=404, detail="User tidak ditemukan")
            if ea_id not in self._user_ea_ids_locked(user):
                raise HTTPException(status_code=404, detail="EA ID tidak terdaftar pada user ini")
            instance = self.state["instances"].get(ea_id)
            if not instance or instance.get("owner") != username:
                raise HTTPException(status_code=404, detail="EA ID tidak ditemukan")
            if update.allowed_ip is not None:
                instance["allowed_ip"] = update.allowed_ip
            if update.daily_profit_auto_stop_enabled is not None:
                instance["daily_profit_auto_stop_enabled"] = update.daily_profit_auto_stop_enabled
                if not update.daily_profit_auto_stop_enabled:
                    instance["daily_profit_auto_stop_triggered"] = False
            if update.daily_profit_target is not None:
                instance["daily_profit_target"] = update.daily_profit_target
            instance["updated_at"] = utc_now()
            self.state["updated_at"] = instance["updated_at"]
            self._save()
            return self._public_user_locked(user)

    def update_daily_profit_auto_stop(
        self,
        user: dict,
        ea_id: str | None,
        update: DailyProfitAutoStopUpdate,
    ) -> dict:
        with self.lock:
            instance = self._instance_for_browser_locked(user, ea_id)
            instance["daily_profit_auto_stop_enabled"] = update.enabled
            instance["daily_profit_target"] = float(update.target or 0.0)
            if not update.enabled:
                instance["daily_profit_auto_stop_triggered"] = False
            instance["updated_at"] = utc_now()
            self.state["updated_at"] = instance["updated_at"]
            self._save()
            return self.dashboard(user, instance["ea_id"])

    def update_user_ea_strategy(
        self,
        username: str,
        ea_id: str,
        update: EaStrategyUpdate,
    ) -> dict:
        username = username.strip().lower()
        with self.lock:
            user = self.state["users"].get(username)
            if not user:
                raise HTTPException(status_code=404, detail="User tidak ditemukan")
            if ea_id not in self._user_ea_ids_locked(user):
                raise HTTPException(status_code=404, detail="EA ID tidak terdaftar pada user ini")
            instance = self.state["instances"].get(ea_id)
            if not instance or instance.get("owner") != username:
                raise HTTPException(status_code=404, detail="EA ID tidak ditemukan")
            if self._instance_is_online_locked(instance):
                raise HTTPException(
                    status_code=409,
                    detail="EA harus offline sebelum jenis EA diubah. Lepas atau hentikan EA di MT5 terlebih dahulu.",
                )
            previous_strategy = instance.get("strategy_type", STRATEGY_STANDARD)
            if previous_strategy == update.strategy_type:
                return self.dashboard(user, instance["ea_id"])
            strategy_configs = instance.get("strategy_configs")
            if not isinstance(strategy_configs, dict):
                strategy_configs = {}
            strategy_configs[previous_strategy] = copy.deepcopy(instance["config"])
            next_config = strategy_configs.get(update.strategy_type)
            try:
                next_config = config_model_for_strategy(update.strategy_type).model_validate(
                    next_config or default_config_for_strategy(update.strategy_type)
                ).model_dump()
            except (TypeError, ValueError):
                next_config = default_config_for_strategy(update.strategy_type)
            active_mode = instance.get("active_trading_mode")
            mode = self.state["trading_modes"].get(active_mode) if active_mode else None
            if mode:
                for field in ("rsi_period", "rsi_buy_level", "rsi_sell_level"):
                    next_config[field] = mode[field]
            instance["strategy_type"] = update.strategy_type
            instance["config"] = next_config
            strategy_configs[update.strategy_type] = copy.deepcopy(next_config)
            instance["strategy_configs"] = strategy_configs
            instance["config_revision"] += 1
            instance["status"] = None
            instance["lease"] = None
            instance["updated_at"] = utc_now()
            self.state["updated_at"] = instance["updated_at"]
            self._save()
            return self._public_user_locked(user)

    def update_user_expiration(self, username: str, update: UserExpirationUpdate) -> dict:
        username = username.strip().lower()
        with self.lock:
            self._expire_due_users_locked()
            user = self.state["users"].get(username)
            if not user:
                raise HTTPException(status_code=404, detail="User tidak ditemukan")
            if user["role"] == "admin":
                raise HTTPException(status_code=400, detail="Administrator tidak memiliki masa berlaku")
            expires_at = normalized_utc_datetime(update.expires_at)
            if expires_at <= self._now_utc():
                raise HTTPException(status_code=422, detail="Masa berlaku harus berada di masa depan")
            user["expires_at"] = expires_at.isoformat()
            user["expired_at"] = None
            self.state["updated_at"] = utc_now()
            self._save()
            return self._public_user_locked(user)

    def update_user(self, username: str, update: UserUpdate) -> dict:
        username = username.strip().lower()
        with self.lock:
            self._expire_due_users_locked()
            user = self.state["users"].get(username)
            if not user:
                raise HTTPException(status_code=404, detail="User tidak ditemukan")
            if update.enabled is False and user["role"] == "admin":
                raise HTTPException(status_code=400, detail="Administrator tidak dapat dinonaktifkan")
            if update.allowed_ip is not None:
                instance = self.state["instances"].get(user["ea_id"])
                if instance:
                    instance["allowed_ip"] = update.allowed_ip
            if update.password is not None:
                user["password_hash"] = hash_password(update.password)
                user["session_version"] += 1
            if update.enabled is not None and user["enabled"] != update.enabled:
                if update.enabled and user["role"] != "admin":
                    if not user.get("expires_at"):
                        raise HTTPException(status_code=409, detail="Atur masa berlaku user sebelum mengaktifkan akun")
                    if self._user_expired_locked(user):
                        raise HTTPException(status_code=409, detail="Perpanjang masa berlaku sebelum mengaktifkan akun")
                user["enabled"] = update.enabled
                user["session_version"] += 1
                user["disabled_reason"] = None if update.enabled else "manual"
                if update.enabled:
                    user["expired_at"] = None
                else:
                    updated_at = utc_now()
                    for ea_id in self._user_ea_ids_locked(user):
                        instance = self.state["instances"].get(ea_id)
                        if not instance:
                            continue
                        if instance.get("algo_enabled"):
                            instance["algo_enabled"] = False
                            instance["command_revision"] += 1
                        instance["status"] = None
                        instance["lease"] = None
                        instance["updated_at"] = updated_at
            self.state["updated_at"] = utc_now()
            self._save()
            return self._public_user_locked(user)

    def delete_user(self, username: str) -> dict:
        username = username.strip().lower()
        with self.lock:
            user = self.state["users"].get(username)
            if not user:
                raise HTTPException(status_code=404, detail="User tidak ditemukan")
            if user["role"] == "admin":
                raise HTTPException(status_code=400, detail="Administrator tidak dapat dihapus")

            ea_ids = self._user_ea_ids_locked(user)
            for ea_id in ea_ids:
                instance = self.state["instances"].get(ea_id)
                if instance and self._instance_is_online_locked(instance):
                    raise HTTPException(
                        status_code=409,
                        detail=f"User tidak dapat dihapus saat EA {ea_id} masih online",
                    )

            del self.state["users"][username]
            for ea_id in ea_ids:
                self.state["instances"].pop(ea_id, None)
            self.state["updated_at"] = utc_now()
            self._save()
            return {
                "deleted": True,
                "username": username,
                "ea_id": ea_ids[0] if ea_ids else None,
                "ea_ids": ea_ids,
            }

    def rotate_api_token(self, username: str, ea_id: str | None = None) -> tuple[dict, str]:
        username = username.strip().lower()
        with self.lock:
            user = self.state["users"].get(username)
            if not user:
                raise HTTPException(status_code=404, detail="User tidak ditemukan")
            target_ea_id = ea_id or user["ea_id"]
            if target_ea_id not in self._user_ea_ids_locked(user):
                raise HTTPException(status_code=404, detail="EA ID tidak terdaftar pada user ini")
            token = create_api_token()
            instance = self.state["instances"][target_ea_id]
            instance["api_token_hash"] = hash_api_token(token)
            instance["api_token_hint"] = token[-6:]
            instance["status"] = None
            instance["lease"] = None
            self.state["updated_at"] = utc_now()
            self._save()
            return self._public_user_locked(user), token

    def change_own_password(self, username: str, request: PasswordChangeRequest) -> tuple[dict, str]:
        username = username.strip().lower()
        with self.lock:
            user = self.state["users"].get(username)
            if not user or not user["enabled"]:
                raise HTTPException(status_code=404, detail="User tidak ditemukan")
            if not verify_password(request.current_password, user["password_hash"]):
                raise HTTPException(status_code=400, detail="Password lama salah")
            user["password_hash"] = hash_password(request.new_password)
            user["session_version"] += 1
            self.state["updated_at"] = utc_now()
            self._save()
            session = create_session_token(
                self.state["auth_secret"],
                user["username"],
                user["session_version"],
            )
            return self._public_user_locked(user), session

    def rotate_own_api_token(
        self,
        username: str,
        current_password: str,
        ea_id: str | None = None,
    ) -> tuple[dict, str]:
        username = username.strip().lower()
        with self.lock:
            user = self.state["users"].get(username)
            if not user or not user["enabled"]:
                raise HTTPException(status_code=404, detail="User tidak ditemukan")
            if not verify_password(current_password, user["password_hash"]):
                raise HTTPException(status_code=400, detail="Password akun salah")
            target_ea_id = ea_id or user["ea_id"]
            if target_ea_id not in self._user_ea_ids_locked(user):
                raise HTTPException(status_code=403, detail="EA ini bukan milik user yang login")
            token = create_api_token()
            instance = self.state["instances"][target_ea_id]
            instance["api_token_hash"] = hash_api_token(token)
            instance["api_token_hint"] = token[-6:]
            instance["status"] = None
            instance["lease"] = None
            self.state["updated_at"] = utc_now()
            self._save()
            return self._public_user_locked(user), token

    def _instance_for_browser_locked(self, user: dict, ea_id: str | None) -> dict:
        target = ea_id or user["ea_id"]
        if user["role"] != "admin" and target not in self._user_ea_ids_locked(user):
            raise HTTPException(status_code=403, detail="EA ini bukan milik user yang login")
        instance = self.state["instances"].get(target)
        if not instance:
            raise HTTPException(status_code=404, detail="EA ID tidak ditemukan")
        return instance

    def _claim_ea_client_locked(self, instance: dict, account_login: str, client_id: str) -> None:
        now = monotonic()
        lease = instance.get("lease")
        if lease and now - float(lease.get("last_seen", 0.0)) <= EA_CLIENT_LEASE_SECONDS:
            same_client = (
                lease.get("account_login") == account_login
                and lease.get("client_id") == client_id
            )
            if not same_client:
                raise HTTPException(
                    status_code=409,
                    detail="EA ID sedang digunakan instance MT5 lain",
                )
        instance["lease"] = {
            "account_login": account_login,
            "client_id": client_id,
            "last_seen": now,
        }

    def _authorize_ea_locked(
        self,
        ea_id: str,
        token: str,
        source_ip: str,
        account_login: str,
        client_id: str,
    ) -> dict:
        self._expire_due_users_locked()
        instance = self.state["instances"].get(ea_id)
        if not instance:
            raise HTTPException(status_code=401, detail="EA ID atau token tidak valid")
        owner = self.state["users"].get(instance["owner"])
        if not owner or not owner["enabled"]:
            raise HTTPException(status_code=403, detail="Akun EA dinonaktifkan")
        if not token or not secrets.compare_digest(instance["api_token_hash"], hash_api_token(token)):
            raise HTTPException(status_code=401, detail="EA ID atau token tidak valid")
        if not ip_is_allowed(source_ip, instance["allowed_ip"]):
            raise HTTPException(status_code=403, detail=f"IP EA {source_ip} tidak diizinkan")
        self._claim_ea_client_locked(instance, account_login, client_id)
        return instance

    def _local_now(self) -> datetime:
        current = self.now_provider()
        if current.tzinfo is None:
            current = current.replace(tzinfo=SCHEDULE_TIMEZONE)
        return current.astimezone(SCHEDULE_TIMEZONE)

    @staticmethod
    def _parse_time(value: str) -> time:
        hour, minute = (int(part) for part in value.split(":"))
        return time(hour=hour, minute=minute)

    def _schedule_phase(self, schedule: dict, current: datetime) -> str:
        if not schedule["enabled"]:
            return "disabled"
        start = self._parse_time(schedule["start_time"])
        stop = self._parse_time(schedule["stop_time"])
        current_time = current.time().replace(tzinfo=None)
        running = start <= current_time < stop if start < stop else current_time >= start or current_time < stop
        return "running" if running else "stopped"

    def _next_transition(self, schedule: dict, current: datetime) -> tuple[str | None, datetime | None]:
        if not schedule["enabled"]:
            return None, None
        events: list[tuple[datetime, str]] = []
        for day_offset in range(3):
            event_date = current.date() + timedelta(days=day_offset)
            for action, field in (("start", "start_time"), ("stop", "stop_time")):
                event_at = datetime.combine(event_date, self._parse_time(schedule[field]), tzinfo=SCHEDULE_TIMEZONE)
                if event_at > current:
                    events.append((event_at, action))
        if not events:
            return None, None
        event_at, action = min(events, key=lambda item: item[0])
        return action, event_at

    def _apply_schedule_locked(self, instance: dict, force: bool = False) -> tuple[datetime, str]:
        current = self._local_now()
        phase = self._schedule_phase(instance["schedule"], current)
        if not force and phase == instance.get("schedule_phase"):
            return current, phase
        instance["schedule_phase"] = phase
        if phase == "disabled":
            instance["schedule_last_action"] = None
            instance["schedule_last_action_at"] = None
        else:
            action = "start" if phase == "running" else "stop"
            daily_target_blocked = bool(instance.get("daily_profit_auto_stop_triggered")) and (
                instance.get("daily_profit_auto_stop_date") == str((instance.get("status") or {}).get("account_profit_date") or "")
            )
            desired_enabled = phase == "running" and not daily_target_blocked
            instance["schedule_last_action"] = action
            instance["schedule_last_action_at"] = current.astimezone(timezone.utc).isoformat()
            if not desired_enabled:
                instance["stop_mode"] = "pause_only"
            if instance["algo_enabled"] != desired_enabled:
                instance["algo_enabled"] = desired_enabled
                instance["command_revision"] += 1
        instance["updated_at"] = current.astimezone(timezone.utc).isoformat()
        self.state["updated_at"] = instance["updated_at"]
        self._save()
        return current, phase

    def _schedule_status_locked(self, instance: dict, current: datetime, phase: str) -> dict:
        next_action, next_at = self._next_transition(instance["schedule"], current)
        return {
            "schedule": {**instance["schedule"], "timezone": SCHEDULE_TIMEZONE_NAME},
            "schedule_phase": phase,
            "schedule_server_time": current.isoformat(),
            "schedule_next_action": next_action,
            "schedule_next_at": next_at.isoformat() if next_at else None,
            "schedule_last_action": instance.get("schedule_last_action"),
            "schedule_last_action_at": instance.get("schedule_last_action_at"),
        }

    @staticmethod
    def _is_online(status: dict | None) -> bool:
        if not status:
            return False
        try:
            last_seen = datetime.fromisoformat(status["last_seen"])
            return (datetime.now(timezone.utc) - last_seen).total_seconds() <= EA_CLIENT_LEASE_SECONDS
        except (KeyError, TypeError, ValueError):
            return False

    def _instance_is_online_locked(self, instance: dict) -> bool:
        if self._is_online(instance.get("status")):
            return True
        lease = instance.get("lease")
        if not lease:
            return False
        try:
            return monotonic() - float(lease.get("last_seen", 0.0)) <= EA_CLIENT_LEASE_SECONDS
        except (TypeError, ValueError):
            return False

    def _apply_daily_profit_auto_stop_locked(self, instance: dict) -> bool:
        """Stop only new entries when this EA instance reaches its daily target."""
        if not instance.get("daily_profit_auto_stop_enabled"):
            return False
        target = float(instance.get("daily_profit_target") or 0.0)
        status = instance.get("status") or {}
        profit = status.get("account_daily_profit")
        profit_date = str(status.get("account_profit_date") or "")
        if target <= 0.0 or not self._is_online(status) or profit is None or not profit_date:
            return False
        if instance.get("daily_profit_auto_stop_date") != profit_date:
            instance["daily_profit_auto_stop_date"] = profit_date
            instance["daily_profit_auto_stop_triggered"] = False
            instance["updated_at"] = utc_now()
            self._save()
        try:
            reached = float(profit) >= target
        except (TypeError, ValueError):
            return False
        if not reached:
            return False
        if instance.get("daily_profit_auto_stop_triggered") and not instance.get("algo_enabled"):
            return False
        instance["daily_profit_auto_stop_triggered"] = True
        if instance.get("algo_enabled"):
            instance["algo_enabled"] = False
            instance["stop_mode"] = "pause_only"
            instance["command_revision"] += 1
        instance["updated_at"] = utc_now()
        self._save()
        return True

    def dashboard(self, user: dict, ea_id: str | None) -> dict:
        with self.lock:
            self._expire_due_users_locked()
            instance = self._instance_for_browser_locked(user, ea_id)
            daily_profit_auto_stopped = self._apply_daily_profit_auto_stop_locked(instance)
            owner_user = self.state["users"].get(instance["owner"], {})
            owner_expiration = parse_saved_datetime(owner_user.get("expires_at"))
            now_utc = self._now_utc()
            current, phase = self._apply_schedule_locked(instance)
            auto_stopped = False
            if self._is_online(instance.get("status")) and instance.get("algo_enabled"):
                if instance["status"].get("account_balance") == 0:
                    instance["algo_enabled"] = False
                    instance["command_revision"] += 1
                    instance["updated_at"] = utc_now()
                    self._save()
                    auto_stopped = True
            browser_config = dict(instance["config"])
            if user["role"] != "admin":
                for field in ("rsi_period", "rsi_buy_level", "rsi_sell_level"):
                    browser_config.pop(field, None)
            active_mode = instance.get("active_trading_mode")
            active_mode_name = (
                self.state["trading_modes"].get(active_mode, {}).get("name")
                if active_mode
                else None
            )
            return {
                "ea_id": instance["ea_id"],
                "owner": instance["owner"],
                "strategy_type": instance.get("strategy_type", STRATEGY_STANDARD),
                "account_expiration": {
                    "enabled": bool(owner_user.get("enabled")),
                    "expires_at": owner_expiration.isoformat() if owner_expiration else None,
                    "expired": self._user_expired_locked(owner_user, now_utc) if owner_user else False,
                    "expiration_required": owner_user.get("role") != "admin" and owner_expiration is None,
                    "unlimited": owner_user.get("role") == "admin",
                    "remaining_seconds": (
                        max(0, int((owner_expiration - now_utc).total_seconds()))
                        if owner_expiration
                        else None
                    ),
                },
                "allowed_ip": instance["allowed_ip"],
                "config": browser_config,
                "active_trading_mode": active_mode_name,
                "rsi_indicator": {
                    "buy_zone_end": instance["config"]["rsi_buy_level"],
                    "sell_zone_start": instance["config"]["rsi_sell_level"],
                },
                "config_revision": instance["config_revision"],
                "command_revision": instance["command_revision"],
                "refresh_protection_revision": instance["refresh_protection_revision"],
                "algo_enabled": instance["algo_enabled"],
                "auto_stopped_due_to_balance": auto_stopped,
                "daily_profit_auto_stop_enabled": bool(instance.get("daily_profit_auto_stop_enabled", False)),
                "daily_profit_target": float(instance.get("daily_profit_target") or 0.0),
                "daily_profit_auto_stop_triggered": bool(instance.get("daily_profit_auto_stop_triggered", False)),
                "daily_profit_auto_stopped_now": daily_profit_auto_stopped,
                "stop_mode": instance["stop_mode"],
                "updated_at": instance["updated_at"],
                "online": self._is_online(instance["status"]),
                "status": instance["status"],
                **self._schedule_status_locked(instance, current, phase),
            }

    def update_config(
        self,
        user: dict,
        ea_id: str | None,
        update: dict,
    ) -> dict:
        if not isinstance(update, dict):
            raise HTTPException(status_code=422, detail="Payload parameter tidak valid")
        with self.lock:
            instance = self._instance_for_browser_locked(user, ea_id)
            strategy_type = instance.get("strategy_type", STRATEGY_STANDARD)
            model = config_model_for_strategy(strategy_type)
            fields = set(model.model_fields)
            supplied_fields = set(update) - {"refresh_protection"}
            unknown_fields = supplied_fields - fields
            if unknown_fields:
                raise HTTPException(
                    status_code=422,
                    detail=f"Parameter tidak cocok untuk EA {strategy_type}: {', '.join(sorted(unknown_fields))}",
                )
            rsi_fields = {"rsi_period", "rsi_buy_level", "rsi_sell_level"}
            if user["role"] != "admin" and supplied_fields & rsi_fields:
                raise HTTPException(status_code=403, detail="Parameter RSI hanya dapat diubah melalui Entry Mode dari admin")
            try:
                refresh_protection = bool(update.get("refresh_protection", False))
                config_data = {**instance["config"], **{field: update[field] for field in supplied_fields}}
                config = model.model_validate(config_data)
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            instance["config"] = config.model_dump()
            strategy_configs = instance.get("strategy_configs")
            if not isinstance(strategy_configs, dict):
                strategy_configs = {}
            strategy_configs[strategy_type] = copy.deepcopy(instance["config"])
            instance["strategy_configs"] = strategy_configs
            active_mode = instance.get("active_trading_mode")
            mode = self.state["trading_modes"].get(active_mode) if active_mode else None
            if mode and any(
                instance["config"][field] != mode[field]
                for field in ("rsi_period", "rsi_buy_level", "rsi_sell_level")
            ):
                instance["active_trading_mode"] = None
            instance["config_revision"] += 1
            if refresh_protection:
                instance["refresh_protection_revision"] += 1
            instance["updated_at"] = utc_now()
            self._save()
            return self.dashboard(user, instance["ea_id"])

    def _trading_mode_rows_locked(self, include_parameters: bool) -> list[dict]:
        modes = sorted(self.state["trading_modes"].values(), key=lambda mode: mode["name"].casefold())
        if include_parameters:
            return [copy.deepcopy(mode) for mode in modes]
        return [{"name": mode["name"]} for mode in modes]

    def list_trading_modes(self, user: dict, ea_id: str | None) -> dict:
        with self.lock:
            instance = self._instance_for_browser_locked(user, ea_id)
            active_mode = instance.get("active_trading_mode")
            active_name = (
                self.state["trading_modes"].get(active_mode, {}).get("name")
                if active_mode
                else None
            )
            return {
                "scope": "global",
                "modes": self._trading_mode_rows_locked(user["role"] == "admin"),
                "active_mode": active_name,
            }

    def save_trading_mode(
        self,
        user: dict,
        request: TradingModeSaveRequest,
    ) -> dict:
        if user["role"] != "admin":
            raise HTTPException(status_code=403, detail="Hanya administrator yang dapat membuat mode trading")
        with self.lock:
            mode_key = request.name.casefold()
            modes = self.state["trading_modes"]
            created = mode_key not in modes
            mode = {**request.model_dump(), "updated_at": utc_now()}
            modes[mode_key] = mode
            for instance in self.state["instances"].values():
                if instance.get("active_trading_mode") != mode_key:
                    continue
                changed = False
                for field in ("rsi_period", "rsi_buy_level", "rsi_sell_level"):
                    if instance["config"][field] != mode[field]:
                        instance["config"][field] = mode[field]
                        changed = True
                if changed:
                    instance.setdefault("strategy_configs", {})[
                        instance.get("strategy_type", STRATEGY_STANDARD)
                    ] = copy.deepcopy(instance["config"])
                    instance["config_revision"] += 1
                    instance["updated_at"] = mode["updated_at"]
            self.state["updated_at"] = mode["updated_at"]
            self._save()
            return {
                "scope": "global",
                "created": created,
                "mode": copy.deepcopy(mode),
                "modes": self._trading_mode_rows_locked(True),
            }

    def delete_trading_mode(self, user: dict, mode_name: str) -> dict:
        if user["role"] != "admin":
            raise HTTPException(status_code=403, detail="Hanya administrator yang dapat menghapus mode trading")
        with self.lock:
            try:
                normalized_name = normalize_trading_mode_name(mode_name)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            mode_key = normalized_name.casefold()
            deleted = self.state["trading_modes"].pop(mode_key, None)
            if not deleted:
                raise HTTPException(status_code=404, detail="Mode trading tidak ditemukan")
            updated_at = utc_now()
            for instance in self.state["instances"].values():
                if instance.get("active_trading_mode") == mode_key:
                    instance["active_trading_mode"] = None
                    instance["updated_at"] = updated_at
            self.state["updated_at"] = updated_at
            self._save()
            return {
                "scope": "global",
                "deleted": True,
                "mode_name": deleted["name"],
                "modes": self._trading_mode_rows_locked(True),
            }

    def activate_trading_mode(
        self,
        user: dict,
        ea_id: str | None,
        request: TradingModeSelection,
    ) -> dict:
        if user["role"] != "user":
            raise HTTPException(status_code=403, detail="Pemilihan mode trading hanya tersedia untuk user biasa")
        with self.lock:
            instance = self._instance_for_browser_locked(user, ea_id)
            mode_key = request.name.casefold()
            mode = self.state["trading_modes"].get(mode_key)
            if not mode:
                raise HTTPException(status_code=404, detail="Mode trading tidak tersedia")
            changed = instance.get("active_trading_mode") != mode_key
            for field in ("rsi_period", "rsi_buy_level", "rsi_sell_level"):
                if instance["config"][field] != mode[field]:
                    instance["config"][field] = mode[field]
                    changed = True
            instance["active_trading_mode"] = mode_key
            if changed:
                instance.setdefault("strategy_configs", {})[
                    instance.get("strategy_type", STRATEGY_STANDARD)
                ] = copy.deepcopy(instance["config"])
                instance["config_revision"] += 1
                instance["updated_at"] = utc_now()
                self.state["updated_at"] = instance["updated_at"]
                self._save()
            return self.dashboard(user, instance["ea_id"])

    def control(self, user: dict, ea_id: str | None, request: ControlRequest) -> dict:
        with self.lock:
            instance = self._instance_for_browser_locked(user, ea_id)
            if request.action == "start" and self._is_online(instance.get("status")):
                if instance["status"].get("account_balance") == 0:
                    raise HTTPException(
                        status_code=409,
                        detail="EA tidak dapat dimulai karena saldo akun saat ini 0.",
                    )
                current_profit_date = str(instance["status"].get("account_profit_date") or "")
                if (
                    instance.get("daily_profit_auto_stop_enabled")
                    and instance.get("daily_profit_auto_stop_triggered")
                    and instance.get("daily_profit_auto_stop_date") == current_profit_date
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="Entry baru sudah dihentikan karena target profit harian tercapai.",
                    )
            instance["algo_enabled"] = request.action == "start"
            instance["stop_mode"] = request.stop_mode
            instance["command_revision"] += 1
            instance["updated_at"] = utc_now()
            self._save()
            return self.dashboard(user, instance["ea_id"])

    def update_schedule(self, user: dict, ea_id: str | None, update: ScheduleConfig) -> dict:
        with self.lock:
            instance = self._instance_for_browser_locked(user, ea_id)
            instance["schedule"] = update.model_dump()
            instance["schedule_phase"] = None if update.enabled else "disabled"
            if update.enabled:
                self._apply_schedule_locked(instance, force=True)
            else:
                instance["schedule_last_action"] = None
                instance["schedule_last_action_at"] = None
                instance["updated_at"] = utc_now()
                self._save()
            return self.dashboard(user, instance["ea_id"])

    def poll_ea(
        self,
        ea_id: str,
        token: str,
        source_ip: str,
        account_login: str,
        client_id: str,
    ) -> dict:
        with self.lock:
            instance = self._authorize_ea_locked(
                ea_id,
                token,
                source_ip,
                account_login,
                client_id,
            )
            self._apply_schedule_locked(instance)
            return self._ea_control_payload_locked(instance)

    @staticmethod
    def _ea_control_payload_locked(instance: dict) -> dict:
        return {
            "ea_id": instance["ea_id"],
            "strategy_type": instance.get("strategy_type", STRATEGY_STANDARD),
            "config_revision": instance["config_revision"],
            "command_revision": instance["command_revision"],
            "refresh_protection_revision": instance["refresh_protection_revision"],
            "algo_enabled": instance["algo_enabled"],
            "stop_mode": instance["stop_mode"],
            **instance["config"],
        }

    def update_ea_status(self, status: EaStatus, token: str, source_ip: str) -> dict:
        with self.lock:
            instance = self._authorize_ea_locked(
                status.ea_id,
                token,
                source_ip,
                status.account_login,
                status.client_id,
            )
            expected_strategy = instance.get("strategy_type", STRATEGY_STANDARD)
            if status.strategy_type != expected_strategy:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Jenis EA tidak cocok. WebUI mengharapkan {expected_strategy}, "
                        f"tetapi MT5 mengirim {status.strategy_type}."
                    ),
                )
            record = status.model_dump()
            record["last_seen"] = utc_now()
            record["source_ip"] = source_ip
            instance["status"] = record
            self._apply_daily_profit_auto_stop_locked(instance)
            self._apply_schedule_locked(instance)
            return {
                "accepted": True,
                "server_time": record["last_seen"],
                **self._ea_control_payload_locked(instance),
            }


def bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Token EA diperlukan")
    token = authorization[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Token EA diperlukan")
    return token


def current_user(request: Request) -> dict:
    user = request.app.state.store.session_user(request.cookies.get(SESSION_COOKIE))
    if not user:
        raise HTTPException(status_code=401, detail="Silakan login")
    return user


def admin_user(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Akses administrator diperlukan")
    return user


def auth_response(payload: dict, session: str) -> JSONResponse:
    response = JSONResponse(payload)
    response.set_cookie(
        SESSION_COOKIE,
        session,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="strict",
        path="/",
    )
    return response


def create_app(
    state_file: Path | None = None,
    now_provider: Callable[[], datetime] | None = None,
) -> FastAPI:
    store = StateStore(state_file or DEFAULT_STATE_FILE, now_provider=now_provider)

    async def expiration_worker() -> None:
        while True:
            store.expire_due_users()
            await asyncio.sleep(1.0)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        expiration_task = asyncio.create_task(expiration_worker())
        try:
            yield
        finally:
            expiration_task.cancel()
            with suppress(asyncio.CancelledError):
                await expiration_task

    api = FastAPI(
        title="RSI Martingale Multiuser Control",
        version="2.5.4.2",
        description="Authenticated multiuser control plane for MT5 RSI Martingale EAs.",
        lifespan=lifespan,
    )
    api.state.store = store

    @api.middleware("http")
    async def disable_response_caching(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

    @api.get("/api/health")
    def health() -> dict:
        return {
            "status": "ok",
            "version": "2.5.4.2",
            "time": utc_now(),
            "setup_required": store.setup_required(),
        }

    @api.get("/api/auth/session")
    def auth_session(request: Request) -> dict:
        if store.setup_required():
            return {"setup_required": True, "authenticated": False, "user": None}
        user = store.session_user(request.cookies.get(SESSION_COOKIE))
        return {"setup_required": False, "authenticated": bool(user), "user": user}

    @api.post("/api/auth/setup")
    def setup(request: AccountCreate) -> JSONResponse:
        user, session, token = store.setup_admin(request)
        return auth_response({"authenticated": True, "user": user, "ea_token": token}, session)

    @api.post("/api/auth/login")
    def login(credentials: LoginRequest, request: Request) -> JSONResponse:
        source_ip = request.client.host if request.client else ""
        user, session = store.login(credentials, source_ip)
        return auth_response({"authenticated": True, "user": user}, session)

    @api.post("/api/auth/logout")
    def logout() -> JSONResponse:
        response = JSONResponse({"authenticated": False})
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @api.get("/api/account")
    def get_account(user: dict = Depends(current_user)) -> dict:
        return user

    @api.post("/api/account/change-password")
    def change_password(
        password_request: PasswordChangeRequest,
        user: dict = Depends(current_user),
    ) -> JSONResponse:
        updated_user, session = store.change_own_password(user["username"], password_request)
        return auth_response({"authenticated": True, "user": updated_user}, session)

    @api.post("/api/account/rotate-token")
    def rotate_own_token(
        verification: PasswordVerificationRequest,
        ea_id: str | None = Query(default=None, min_length=3, max_length=100, pattern=EA_ID_PATTERN),
        user: dict = Depends(current_user),
    ) -> dict:
        updated_user, token = store.rotate_own_api_token(
            user["username"],
            verification.current_password,
            ea_id,
        )
        return {"user": updated_user, "ea_id": ea_id or updated_user["ea_id"], "ea_token": token}

    @api.get("/api/admin/users")
    def get_users(_: dict = Depends(admin_user)) -> list[dict]:
        return store.list_users()

    @api.post("/api/admin/users")
    def post_user(request: AccountCreate, _: dict = Depends(admin_user)) -> dict:
        user, token = store.create_user(request)
        return {"user": user, "ea_id": request.ea_id, "ea_token": token}

    @api.patch("/api/admin/users/{username}")
    def patch_user(username: str, update: UserUpdate, _: dict = Depends(admin_user)) -> dict:
        return store.update_user(username, update)

    @api.put("/api/admin/users/{username}/expiration")
    def put_user_expiration(
        username: str,
        update: UserExpirationUpdate,
        _: dict = Depends(admin_user),
    ) -> dict:
        return store.update_user_expiration(username, update)

    @api.delete("/api/admin/users/{username}")
    def delete_user(username: str, _: dict = Depends(admin_user)) -> dict:
        return store.delete_user(username)

    @api.post("/api/admin/users/{username}/rotate-token")
    def rotate_token(username: str, _: dict = Depends(admin_user)) -> dict:
        user, token = store.rotate_api_token(username)
        return {"user": user, "ea_id": user["ea_id"], "ea_token": token}

    @api.post("/api/admin/users/{username}/eas")
    def post_user_ea(
        username: str,
        request: EaCreateRequest,
        _: dict = Depends(admin_user),
    ) -> dict:
        user, token = store.add_ea_to_user(username, request)
        return {"user": user, "ea_id": request.ea_id, "ea_token": token}

    @api.patch("/api/admin/users/{username}/eas/{ea_id}")
    def patch_user_ea(
        username: str,
        ea_id: str,
        update: EaUpdateRequest,
        _: dict = Depends(admin_user),
    ) -> dict:
        return store.update_user_ea(username, ea_id, update)

    @api.put("/api/admin/users/{username}/eas/{ea_id}/strategy")
    def put_user_ea_strategy(
        username: str,
        ea_id: str,
        update: EaStrategyUpdate,
        _: dict = Depends(admin_user),
    ) -> dict:
        return store.update_user_ea_strategy(username, ea_id, update)

    @api.post("/api/admin/users/{username}/eas/{ea_id}/rotate-token")
    def rotate_user_ea_token(
        username: str,
        ea_id: str,
        _: dict = Depends(admin_user),
    ) -> dict:
        user, token = store.rotate_api_token(username, ea_id)
        return {"user": user, "ea_id": ea_id, "ea_token": token}

    @api.get("/api/ea/poll")
    def poll_ea(
        request: Request,
        ea_id: str = Query(min_length=3, max_length=100, pattern=EA_ID_PATTERN),
        account_login: str = Query(min_length=1, max_length=40),
        client_id: str = Query(min_length=3, max_length=100, pattern=EA_ID_PATTERN),
        authorization: str | None = Header(default=None),
    ) -> dict:
        source_ip = request.client.host if request.client else ""
        return store.poll_ea(
            ea_id,
            bearer_token(authorization),
            source_ip,
            account_login,
            client_id,
        )

    @api.post("/api/ea/status")
    def post_ea_status(
        status: EaStatus,
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> dict:
        source_ip = request.client.host if request.client else ""
        return store.update_ea_status(status, bearer_token(authorization), source_ip)

    @api.get("/api/dashboard")
    def get_dashboard(
        ea_id: str | None = Query(default=None, min_length=3, max_length=100, pattern=EA_ID_PATTERN),
        user: dict = Depends(current_user),
    ) -> dict:
        return store.dashboard(user, ea_id)

    @api.get("/api/dashboard/stream")
    async def stream_dashboard(
        request: Request,
        ea_id: str | None = Query(default=None, min_length=3, max_length=100, pattern=EA_ID_PATTERN),
        user: dict = Depends(current_user),
    ) -> StreamingResponse:
        session_cookie = request.cookies.get(SESSION_COOKIE)

        async def dashboard_events():
            last_signature: tuple | None = None
            last_keepalive = monotonic()
            last_session_check = 0.0
            while not await request.is_disconnected():
                now = monotonic()
                if now - last_session_check >= 5.0:
                    if not store.session_user(session_cookie):
                        break
                    last_session_check = now
                try:
                    data = store.dashboard(user, ea_id)
                except HTTPException:
                    break
                status = data.get("status") or {}
                expiration = data.get("account_expiration") or {}
                signature = (
                    status.get("last_seen"),
                    data["online"],
                    data["config_revision"],
                    data["command_revision"],
                    data["refresh_protection_revision"],
                    data["algo_enabled"],
                    data["stop_mode"],
                    data.get("schedule_phase"),
                    data.get("schedule_last_action_at"),
                    data.get("active_trading_mode"),
                    expiration.get("enabled"),
                    expiration.get("expires_at"),
                    expiration.get("expired"),
                    expiration.get("expiration_required"),
                )
                if signature != last_signature:
                    yield f"data: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"
                    last_signature = signature
                    last_keepalive = now
                elif now - last_keepalive >= 10.0:
                    yield ": keepalive\n\n"
                    last_keepalive = now
                await asyncio.sleep(0.2)

        return StreamingResponse(
            dashboard_events(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-store, max-age=0",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @api.get("/api/trading-modes")
    def get_trading_modes(
        ea_id: str | None = Query(default=None, min_length=3, max_length=100, pattern=EA_ID_PATTERN),
        user: dict = Depends(current_user),
    ) -> dict:
        return store.list_trading_modes(user, ea_id)

    @api.post("/api/trading-modes")
    def post_trading_mode(
        mode: TradingModeSaveRequest,
        user: dict = Depends(admin_user),
    ) -> dict:
        return store.save_trading_mode(user, mode)

    @api.delete("/api/trading-modes")
    def delete_trading_mode(
        mode_name: str = Query(min_length=1, max_length=40),
        user: dict = Depends(admin_user),
    ) -> dict:
        return store.delete_trading_mode(user, mode_name)

    @api.put("/api/trading-mode/active")
    def put_active_trading_mode(
        selection: TradingModeSelection,
        ea_id: str | None = Query(default=None, min_length=3, max_length=100, pattern=EA_ID_PATTERN),
        user: dict = Depends(current_user),
    ) -> dict:
        return store.activate_trading_mode(user, ea_id, selection)

    @api.put("/api/config")
    def put_config(
        update: dict,
        ea_id: str | None = Query(default=None, min_length=3, max_length=100, pattern=EA_ID_PATTERN),
        user: dict = Depends(current_user),
    ) -> dict:
        return store.update_config(user, ea_id, update)

    @api.patch("/api/daily-profit-auto-stop")
    def patch_daily_profit_auto_stop(
        update: DailyProfitAutoStopUpdate,
        ea_id: str | None = Query(default=None, min_length=3, max_length=100, pattern=EA_ID_PATTERN),
        user: dict = Depends(current_user),
    ) -> dict:
        return store.update_daily_profit_auto_stop(user, ea_id, update)

    @api.post("/api/control")
    def post_control(
        request: ControlRequest,
        ea_id: str | None = Query(default=None, min_length=3, max_length=100, pattern=EA_ID_PATTERN),
        user: dict = Depends(current_user),
    ) -> dict:
        if request.action == "start" and request.stop_mode == "close_all":
            request.stop_mode = "pause_only"
        return store.control(user, ea_id, request)

    @api.put("/api/schedule")
    def put_schedule(
        update: ScheduleConfig,
        ea_id: str | None = Query(default=None, min_length=3, max_length=100, pattern=EA_ID_PATTERN),
        user: dict = Depends(current_user),
    ) -> dict:
        return store.update_schedule(user, ea_id, update)

    @api.get("/", include_in_schema=False)
    def index() -> FileResponse:
        index_path = STATIC_DIR / "index.html"
        if not index_path.exists():
            raise HTTPException(status_code=404, detail="WebUI belum tersedia")
        return FileResponse(index_path)

    api.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return api


app = create_app()
