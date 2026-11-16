"""GeoJSON / CSV 输入解析。

两种来源最终都产出 :class:`geo_ingest.event.Event`，几何在构造阶段即完成
CRS 规范化，因此后续分区逻辑完全不需要关心输入文件格式。
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Iterable

from .crs import EPSG_4326, EPSG_3857, SUPPORTED_SRIDS
from .errors import InvalidEventError, InvalidGeometryError
from .event import Event
from .geometry import Geometry

_ID_KEYS = ("event_id", "id")
_TIME_KEYS = ("updated_at", "timestamp", "event_time", "time")
_WKT_KEYS = ("wkt", "geometry_wkt")
_LON_KEYS = ("lon", "longitude", "lng", "x")
_LAT_KEYS = ("lat", "latitude", "y")


def _pick(mapping: dict, keys: tuple[str, ...]):
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    return None


def _extract(properties: dict, keys: tuple[str, ...], label: str):
    value = _pick(properties, keys)
    if value is None:
        raise InvalidEventError(f"缺少必填字段 {label}，候选列: {', '.join(keys)}")
    return value


def _resolve_srid(explicit: int | None, properties: dict) -> int:
    if explicit is not None:
        if explicit not in SUPPORTED_SRIDS:
            raise InvalidEventError(f"不支持的 SRID: {explicit}")
        return explicit
    raw = _pick(properties, ("srid", "crs"))
    if raw is None:
        return EPSG_4326
    try:
        srid = int(str(raw).removeprefix("EPSG:"))
    except ValueError as exc:
        raise InvalidEventError(f"无法解析 SRID: {raw!r}") from exc
    if srid not in SUPPORTED_SRIDS:
        raise InvalidEventError(f"不支持的 SRID: {srid}")
    return srid


# ---- GeoJSON -------------------------------------------------------------

def parse_geojson(
    payload: dict | str | list,
    srid: int | None = None,
    precision: int | None = None,
) -> list[Event]:
    """解析 GeoJSON ``FeatureCollection`` / ``Feature`` / 几何对象。

    RFC 7946 默认 CRS 为 EPSG:4326；也兼容旧式 ``crs`` 成员声明 3857，
    或通过 ``srid`` 参数显式指定（优先级最高）。
    """
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, (dict, list)):
        raise InvalidGeometryError("GeoJSON 根节点必须是对象或数组")

    features, declared_srid = _iter_features(payload)
    chosen_srid = srid if srid is not None else declared_srid
    events = []
    for feature in features:
        if not isinstance(feature, dict) or feature.get("type") != "Feature":
            raise InvalidGeometryError("GeoJSON 数组元素必须是 Feature")
        properties = dict(feature.get("properties") or {})
        feature_srid = _resolve_srid(chosen_srid, properties)
        event_precision = precision if precision is not None else _precision(properties)
        geometry = Geometry.from_geojson(
            feature.get("geometry") or {},
            srid=feature_srid,
            precision=event_precision,
        )
        event_id = _extract(properties, _ID_KEYS, "event_id")
        updated_at = _extract(properties, _TIME_KEYS, "updated_at")
        events.append(
            Event.create(event_id, geometry, updated_at, _clean(properties))
        )
    return events


def _iter_features(payload):
    if isinstance(payload, list):
        return payload, EPSG_4326
    geom_type = payload.get("type")
    if geom_type == "FeatureCollection":
        return payload.get("features") or [], _crs_srid(payload.get("crs"))
    if geom_type == "Feature":
        return [payload], _crs_srid(payload.get("crs"))
    if geom_type in ("Point", "LineString", "Polygon"):
        feature = {"type": "Feature", "geometry": payload, "properties": {}}
        return [feature], EPSG_4326
    raise InvalidGeometryError(f"无法识别的 GeoJSON 类型: {geom_type!r}")


def _crs_srid(crs: Any) -> int:
    if not crs:
        return EPSG_4326
    name = ""
    if isinstance(crs, dict):
        properties = crs.get("properties") or {}
        name = str(properties.get("name", ""))
    else:
        name = str(crs)
    upper = name.upper()
    if "3857" in upper:
        return EPSG_3857
    if "4326" in upper or "CRS84" in upper:
        return EPSG_4326
    raise InvalidGeometryError(f"不支持的 GeoJSON crs 声明: {name!r}")


# ---- CSV -----------------------------------------------------------------

def parse_csv(
    rows: Iterable[str] | Iterable[dict],
    srid: int | None = None,
    precision: int | None = None,
) -> list[Event]:
    """解析带表头的 CSV。

    几何两种写法二选一：

    * ``wkt`` 列：POINT / LINESTRING / POLYGON 的 WKT 文本；
    * ``lon`` + ``lat`` 列（也接受 longitude/latitude、lng/x 等别名），
      生成 Point 几何。

    必填业务列为 ``event_id``（或 ``id``）与 ``updated_at``
    （或 ``timestamp`` / ``event_time`` / ``time``）。
    """
    records = _as_dict_rows(rows)
    events = []
    for row_number, row in enumerate(records, start=2):
        try:
            events.append(_parse_csv_row(row, srid, precision))
        except Exception as exc:
            raise type(exc)(f"CSV 第 {row_number} 行解析失败: {exc}") from exc
    return events


def _as_dict_rows(rows):
    if rows is None:
        return []
    if isinstance(rows, csv.DictReader):
        return list(rows)
    first = next(iter(rows), None)
    if first is None:
        return []
    if isinstance(first, dict):
        return list(rows)
    if isinstance(first, str):
        return list(csv.DictReader(rows))
    raise InvalidEventError("parse_csv 需要字符串行或 dict 行的可迭代对象")


def _parse_csv_row(row: dict, srid: int | None, precision: int | None) -> Event:
    properties = {key: value for key, value in row.items() if value not in (None, "")}
    row_srid = _resolve_srid(srid, properties)
    row_precision = precision if precision is not None else _precision(properties)

    wkt = _pick(properties, _WKT_KEYS)
    if wkt is not None:
        geometry = Geometry.from_wkt(str(wkt), srid=row_srid, precision=row_precision)
    else:
        lon = _extract(properties, _LON_KEYS, "lon")
        lat = _extract(properties, _LAT_KEYS, "lat")
        geometry = Geometry(
            "Point", [lon, lat], srid=row_srid, precision=row_precision
        )

    event_id = _extract(properties, _ID_KEYS, "event_id")
    updated_at = _extract(properties, _TIME_KEYS, "updated_at")
    return Event.create(event_id, geometry, updated_at, _clean(properties))


def _precision(properties: dict) -> int:
    raw = _pick(properties, ("precision", "coord_precision"))
    if raw is None:
        from .crs import DEFAULT_PRECISION

        return DEFAULT_PRECISION
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise InvalidEventError(f"非法坐标精度: {raw!r}") from exc
    if value < 0:
        raise InvalidEventError(f"非法坐标精度: {raw!r}")
    return value


def _clean(properties: dict) -> dict:
    """移除控制字段，只保留业务属性。"""
    reserved = set(_ID_KEYS) | set(_TIME_KEYS) | set(_WKT_KEYS)
    reserved |= set(_LON_KEYS) | set(_LAT_KEYS)
    reserved |= {"srid", "crs", "precision", "coord_precision"}
    return {key: value for key, value in properties.items() if key not in reserved}


# ---- 文件辅助 -------------------------------------------------------------

def parse_geojson_file(path: str | Path, srid: int | None = None,
                       precision: int | None = None) -> list[Event]:
    with open(path, "r", encoding="utf-8") as handle:
        return parse_geojson(json.load(handle), srid=srid, precision=precision)


def parse_csv_file(path: str | Path, srid: int | None = None,
                   precision: int | None = None) -> list[Event]:
    with open(path, "r", encoding="utf-8", newline="") as handle:
        return parse_csv(csv.DictReader(handle), srid=srid, precision=precision)
