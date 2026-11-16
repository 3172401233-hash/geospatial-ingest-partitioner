"""车载事件模型与版本判定。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from .errors import InvalidEventError
from .geometry import Geometry


def parse_timestamp(value: Any) -> datetime:
    """把常见时间表示解析为带 UTC 时区的 ``datetime``。

    支持 epoch 秒（int/float）和 ISO 8601 字符串（``Z`` 后缀同样支持）；
    无时区信息的时间戳按 UTC 处理。
    """
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, bool):
        raise InvalidEventError(f"非法更新时间: {value!r}")
    elif isinstance(value, (int, float)):
        dt = datetime.fromtimestamp(value, tz=timezone.utc)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise InvalidEventError("更新时间为空字符串")
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise InvalidEventError(f"无法解析更新时间: {value!r}") from exc
    else:
        raise InvalidEventError(f"不支持的更新时间类型: {type(value).__name__}")

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def canonical_hash(geometry: Geometry, properties: dict) -> str:
    """事件内容的稳定哈希，用于同一时间戳下的确定性决胜。"""
    payload = {
        "geometry": geometry.to_geojson(),
        "properties": properties,
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=_json_default,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _json_default(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"无法序列化类型: {type(value).__name__}")


@dataclass(frozen=True)
class Event:
    """一条规范化后的车载事件。

    属性
    ------
    event_id:
        业务主键，同 ID 的事件只保留一个物理版本。
    geometry:
        已规范化到 EPSG:4326 的几何。
    updated_at:
        带 UTC 时区的更新时间，迟到更新据此排序。
    properties:
        其余业务字段，必须可 JSON 序列化。
    """

    event_id: str
    geometry: Geometry
    updated_at: datetime
    properties: dict = field(default_factory=dict)
    content_hash: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        event_id = str(self.event_id).strip() if self.event_id is not None else ""
        if not event_id:
            raise InvalidEventError("event_id 不能为空")
        if not isinstance(self.geometry, Geometry):
            raise InvalidEventError("geometry 必须是 Geometry 实例")
        if not isinstance(self.updated_at, datetime) or self.updated_at.tzinfo is None:
            raise InvalidEventError("updated_at 必须是带时区的 datetime")
        object.__setattr__(self, "event_id", event_id)
        object.__setattr__(
            self, "updated_at", self.updated_at.astimezone(timezone.utc)
        )
        object.__setattr__(self, "properties", dict(self.properties))
        if not self.content_hash:
            object.__setattr__(
                self,
                "content_hash",
                canonical_hash(self.geometry, self.properties),
            )

    @classmethod
    def create(
        cls,
        event_id,
        geometry: Geometry,
        updated_at,
        properties: dict | None = None,
    ) -> "Event":
        """从原始时间表示构造事件，自动补齐内容哈希。"""
        return cls(
            event_id=event_id,
            geometry=geometry,
            updated_at=parse_timestamp(updated_at),
            properties=dict(properties or {}),
        )

    def version_token(self) -> tuple[datetime, str]:
        """版本令牌：先比更新时间，再比内容哈希，保证全序可决胜。"""
        return (self.updated_at, self.content_hash)

    def is_newer_than(self, other: "Event") -> bool:
        return self.version_token() > other.version_token()

    def to_record(self) -> dict:
        """转成可 JSON 序列化的记录。"""
        return {
            "event_id": self.event_id,
            "updated_at": self.updated_at.isoformat(),
            "geometry": self.geometry.to_geojson(),
            "properties": _jsonable(self.properties),
            "content_hash": self.content_hash,
        }


def _jsonable(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value
