"""地理事件接入层的异常类型。"""


class GeoIngestError(Exception):
    """所有接入层异常的基类。"""


class InvalidCoordinateError(GeoIngestError):
    """坐标越界、NaN/无穷大或无法解析。"""


class InvalidGeometryError(GeoIngestError):
    """几何类型不支持、坐标层级错误或环不闭合。"""


class InvalidEventError(GeoIngestError):
    """事件缺少必填字段（event_id、geometry、updated_at 等）。"""
