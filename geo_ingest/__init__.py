"""地理事件接入分区库。

纯标准库实现，负责把 GeoJSON / CSV 车载事件规范化到统一 CRS，
按固定网格生成稳定的物理分区键，并维护跨网格的空间索引与迟到更新。
"""

from .errors import (
    GeoIngestError,
    InvalidCoordinateError,
    InvalidGeometryError,
    InvalidEventError,
)
from .crs import EPSG_4326, EPSG_3857, SUPPORTED_SRIDS, normalize_coordinate
from .geometry import Geometry
from .grid import Grid
from .event import Event
from .partitioner import EventPartitionStore
from .parsers import parse_geojson, parse_geojson_file, parse_csv, parse_csv_file

__all__ = [
    "GeoIngestError",
    "InvalidCoordinateError",
    "InvalidGeometryError",
    "InvalidEventError",
    "EPSG_4326",
    "EPSG_3857",
    "SUPPORTED_SRIDS",
    "normalize_coordinate",
    "Geometry",
    "Grid",
    "Event",
    "EventPartitionStore",
    "parse_geojson",
    "parse_geojson_file",
    "parse_csv",
    "parse_csv_file",
]
