"""规范化几何对象。

支持三种 OGC 几何：``Point`` / ``LineString`` / ``Polygon``。
所有坐标在构造时即通过 :func:`geo_ingest.crs.normalize_coordinate`
规范化为 EPSG:4326 的固定精度 ``Decimal``。

日期变更线（antimeridian）不做几何切割：网格计算在“解绕经度”空间进行
（见 :meth:`Geometry.unwrapped_coordinates`），索引键再按 360 度环绕折叠，
因此一条跨越 ±180° 的线会落到日期变更线两侧的相邻单元格中。
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Sequence

from .crs import DEFAULT_PRECISION, EPSG_4326, normalize_coordinate
from .errors import InvalidGeometryError

POINT = "Point"
LINESTRING = "LineString"
POLYGON = "Polygon"
_GEOMETRY_TYPES = frozenset({POINT, LINESTRING, POLYGON})

LonLat = tuple[Decimal, Decimal]


class Geometry:
    """不可变的规范化几何。

    ``coordinates`` 的层级与 GeoJSON 一致：

    * Point: ``[lon, lat]``
    * LineString: ``[[lon, lat], ...]``
    * Polygon: ``[[[lon, lat, ...], 外环], [内环 ...]]``
    """

    __slots__ = ("geom_type", "coordinates", "srid", "precision")

    def __init__(
        self,
        geom_type: str,
        coordinates,
        srid: int = EPSG_4326,
        precision: int = DEFAULT_PRECISION,
    ) -> None:
        if geom_type not in _GEOMETRY_TYPES:
            raise InvalidGeometryError(f"不支持的几何类型: {geom_type!r}")
        self.geom_type = geom_type
        self.srid = srid
        self.precision = precision
        self.coordinates = self._normalize(coordinates, srid, precision)

    # ---- 构造与解析 -----------------------------------------------------

    def _normalize(self, coordinates, srid, precision):
        if self.geom_type == POINT:
            return self._norm_point(coordinates, srid, precision)
        if self.geom_type == LINESTRING:
            ring = [self._norm_point(p, srid, precision) for p in coordinates]
            if len(ring) < 2:
                raise InvalidGeometryError("LineString 至少需要两个坐标点")
            return ring
        rings = []
        if not coordinates:
            raise InvalidGeometryError("Polygon 至少需要一个外环")
        for ring in coordinates:
            if len(ring) < 4:
                raise InvalidGeometryError("多边形环至少需要四个坐标点（含闭合点）")
            normalized = [self._norm_point(p, srid, precision) for p in ring]
            if normalized[0] != normalized[-1]:
                raise InvalidGeometryError("多边形环必须首尾闭合")
            rings.append(normalized)
        return rings

    @staticmethod
    def _norm_point(point, srid, precision) -> LonLat:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            raise InvalidGeometryError(f"坐标点必须是 [lon, lat]: {point!r}")
        return normalize_coordinate(point[0], point[1], srid=srid, precision=precision)

    @classmethod
    def from_geojson(cls, obj: dict, srid: int = EPSG_4326,
                     precision: int = DEFAULT_PRECISION) -> "Geometry":
        """从 GeoJSON geometry 对象构造。"""
        if not isinstance(obj, dict):
            raise InvalidGeometryError("GeoJSON geometry 必须是对象")
        geom_type = obj.get("type")
        coordinates = obj.get("coordinates")
        if geom_type not in _GEOMETRY_TYPES:
            raise InvalidGeometryError(f"不支持的 GeoJSON 几何类型: {geom_type!r}")
        if coordinates is None:
            raise InvalidGeometryError("GeoJSON geometry 缺少 coordinates")
        return cls(geom_type, coordinates, srid=srid, precision=precision)

    @classmethod
    def from_wkt(cls, wkt: str, srid: int = EPSG_4326,
                 precision: int = DEFAULT_PRECISION) -> "Geometry":
        """解析 WKT（POINT / LINESTRING / POLYGON，支持 EMPTY 以外的常见写法）。"""
        if not isinstance(wkt, str):
            raise InvalidGeometryError("WKT 必须是字符串")
        text = wkt.strip()
        upper = text.upper()
        match = re.match(r"^(POINT|LINESTRING|POLYGON)(?:\s*(?:Z|M|ZM))?\s*\(", upper)
        if not match:
            raise InvalidGeometryError(f"无法解析的 WKT: {wkt!r}")
        geom_type = {
            "POINT": POINT,
            "LINESTRING": LINESTRING,
            "POLYGON": POLYGON,
        }[match.group(1)]
        open_paren = text.index("(", match.start())
        body = text[open_paren + 1:].rstrip(")").strip()

        if geom_type == POINT:
            return cls(POINT, _parse_wkt_position(body), srid, precision)
        if geom_type == LINESTRING:
            positions = [
                _parse_wkt_position(part)
                for part in _split_top_level(body, ",")
            ]
            return cls(LINESTRING, positions, srid, precision)
        rings = []
        for ring_body in re.split(r"\)\s*,\s*\(", body):
            ring_body = ring_body.strip("() ")
            rings.append([
                _parse_wkt_position(part)
                for part in _split_top_level(ring_body, ",")
            ])
        return cls(POLYGON, rings, srid, precision)

    # ---- 结构访问 -------------------------------------------------------

    def anchor(self) -> LonLat:
        """物理归属点：Point 取自身，LineString/Polygon 取第一个坐标点。

        物理分区键只由该点决定，保证一条记录在任何时候都只有一个归属分区。
        """
        if self.geom_type == POINT:
            return self.coordinates
        if self.geom_type == LINESTRING:
            return self.coordinates[0]
        return self.coordinates[0][0]

    def rings(self) -> list[list[LonLat]]:
        """以环列表的形式返回坐标（Point / LineString 退化为单环）。"""
        if self.geom_type == POINT:
            return [[self.coordinates]]
        if self.geom_type == LINESTRING:
            return [list(self.coordinates)]
        return [list(ring) for ring in self.coordinates]

    def unwrapped_coordinates(self):
        """返回解绕日期变更线后的坐标副本，层级与 ``coordinates`` 相同。

        相邻点经度差超过 180° 时，后续点整体平移 ±360°，使线段不再跨越
        ±180° 边界。解绕只影响网格覆盖计算，不改变规范化后的存储值。
        """
        if self.geom_type == POINT:
            return self.coordinates
        if self.geom_type == LINESTRING:
            return _unwrap_ring(self.coordinates)
        return [_unwrap_ring(ring) for ring in self.coordinates]

    def bbox_unwrapped(self) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        """解绕空间中的包围盒 ``(min_lon, min_lat, max_lon, max_lat)``。"""
        points = list(self._iter_points(self.unwrapped_coordinates()))
        lons = [p[0] for p in points]
        lats = [p[1] for p in points]
        return min(lons), min(lats), max(lons), max(lats)

    @staticmethod
    def _iter_points(coordinates):
        if coordinates and isinstance(coordinates[0], Decimal):
            yield coordinates
            return
        for item in coordinates:
            if item and isinstance(item[0], Decimal):
                yield item
            else:
                yield from Geometry._iter_points(item)

    # ---- 序列化 ---------------------------------------------------------

    def to_geojson(self) -> dict:
        return {
            "type": self.geom_type,
            "coordinates": _to_jsonable(self.coordinates),
        }

    def to_wkt(self) -> str:
        if self.geom_type == POINT:
            lon, lat = self.coordinates
            return f"POINT ({_fixed(lon)} {_fixed(lat)})"
        if self.geom_type == LINESTRING:
            body = ", ".join(
                f"{_fixed(lon)} {_fixed(lat)}" for lon, lat in self.coordinates
            )
            return f"LINESTRING ({body})"
        ring_bodies = [
            ", ".join(f"{_fixed(lon)} {_fixed(lat)}" for lon, lat in ring)
            for ring in self.coordinates
        ]
        body = ", ".join(f"({ring_body})" for ring_body in ring_bodies)
        return f"POLYGON ({body})"

    def __eq__(self, other) -> bool:
        return (
            isinstance(other, Geometry)
            and self.geom_type == other.geom_type
            and self.coordinates == other.coordinates
        )

    def __hash__(self) -> int:
        return hash((self.geom_type, _freeze(self.coordinates)))

    def __repr__(self) -> str:
        return f"Geometry({self.geom_type!r}, {self.to_wkt()})"


# ---- WKT 辅助 -----------------------------------------------------------

def _parse_wkt_position(text: str) -> tuple[str, str]:
    parts = text.strip().split()
    if len(parts) < 2:
        raise InvalidGeometryError(f"非法 WKT 坐标点: {text!r}")
    return parts[0], parts[1]


def _split_top_level(text: str, sep: str) -> list[str]:
    return [part for part in text.split(sep) if part.strip()]


# ---- 日期变更线解绕 ------------------------------------------------------

_HALF_WORLD = Decimal("180")
_FULL_WORLD = Decimal("360")


def _unwrap_ring(ring: Sequence[LonLat]) -> list[LonLat]:
    if len(ring) < 2:
        return list(ring)
    # 累计平移解绕：每一步都基于上一个“已解绕”的经度判断是否跨过
    # ±180°。闭合环把首点同时作为终点参与判断，保证环在解绕空间闭合。
    result = [(ring[0][0], ring[0][1])]
    previous_lon = ring[0][0]
    for lon, lat in ring[1:]:
        delta = lon - previous_lon
        if delta > _HALF_WORLD:
            result.append((lon - _FULL_WORLD, lat))
        elif delta < -_HALF_WORLD:
            result.append((lon + _FULL_WORLD, lat))
        else:
            result.append((lon, lat))
        previous_lon = result[-1][0]
    return result


# ---- 点在多边形内（Decimal 射线法）---------------------------------------

def point_in_ring(point: LonLat, ring: Sequence[LonLat]) -> bool:
    """射线法判断点是否在环内（含边界），坐标必须处于同一解绕空间。"""
    lon, lat = point
    inside = False
    count = len(ring)
    j = count - 1
    for i in range(count):
        lon_i, lat_i = ring[i]
        lon_j, lat_j = ring[j]
        if (lat_i > lat) != (lat_j > lat):
            cross_lon = lon_j + (lat - lat_j) * (lon_i - lon_j) / (lat_i - lat_j)
            if cross_lon > lon:
                inside = not inside
        if lon_i == lon and lat_i == lat:
            return True
        j = i
    return inside


def point_in_polygon(point: LonLat, rings: Sequence[Sequence[LonLat]]) -> bool:
    """点在外环内且不在任何内环中。"""
    if not point_in_ring(point, rings[0]):
        return False
    return not any(point_in_ring(point, ring) for ring in rings[1:])


# ---- 序列化辅助 ----------------------------------------------------------

def _to_jsonable(value):
    if isinstance(value, Decimal):
        return _fixed(value)
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(item) for item in value]
    return value


def _freeze(value):
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _fixed(value: Decimal) -> str:
    """定点格式输出，规范化坐标不会出现科学计数法（如 ``0E-7``）。"""
    return format(value, "f")
