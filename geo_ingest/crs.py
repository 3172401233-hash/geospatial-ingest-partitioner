"""坐标参考系与坐标规范化。

支持的 SRID：

* ``4326`` —— WGS84 经纬度，经度 ``[-180, 180]``、纬度 ``[-90, 90]``。
* ``3857`` —— Web Mercator 投影坐标，单位米。

所有输入最终都规范化为 EPSG:4326 的 ``Decimal`` 经纬度，并按固定小数位
量化，保证相同的物理坐标无论来源文件如何表示，都得到完全相同的值与分区键。
"""

from __future__ import annotations

import math
from decimal import Decimal, ROUND_HALF_EVEN, localcontext

from .errors import InvalidCoordinateError

EPSG_4326 = 4326
EPSG_3857 = 3857
SUPPORTED_SRIDS = frozenset({EPSG_4326, EPSG_3857})

MAX_LON = Decimal("180")
MAX_LAT = Decimal("90")
DEFAULT_PRECISION = 7

_R = Decimal("6378137")
_PI = Decimal(str(math.pi))


def _to_decimal(value) -> Decimal:
    """把数字或字符串安全转成 Decimal。

    ``float`` 先经过 ``str``，避免二进制浮点误差被原样放大；
    非有限值（NaN / Infinity）直接拒绝。
    """
    if isinstance(value, Decimal):
        dec = value
    elif isinstance(value, bool):
        raise InvalidCoordinateError(f"布尔值不是合法坐标: {value!r}")
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise InvalidCoordinateError(f"坐标必须是有限数: {value!r}")
        dec = Decimal(str(value))
    elif isinstance(value, int):
        dec = Decimal(value)
    elif isinstance(value, str):
        text = value.strip()
        try:
            dec = Decimal(text)
        except Exception as exc:
            raise InvalidCoordinateError(f"无法解析坐标: {value!r}") from exc
    else:
        raise InvalidCoordinateError(f"不支持的坐标类型: {type(value).__name__}")
    if not dec.is_finite():
        raise InvalidCoordinateError(f"坐标必须是有限数: {value!r}")
    return dec


def _web_mercator_to_lonlat(x: Decimal, y: Decimal) -> tuple[Decimal, Decimal]:
    """EPSG:3857 米制坐标反投影为 EPSG:4326 经纬度。"""
    with localcontext() as ctx:
        ctx.prec = 40
        lon = (x / _R) * Decimal(180) / _PI
        # y/R = ln(tan(pi/4 + lat/2))，取指数后用 arctan 还原纬度
        lat_rad = Decimal(2) * _atan((y / _R).exp()) - _PI / Decimal(2)
        lat = lat_rad * Decimal(180) / _PI
    return lon, lat


def _atan(value: Decimal) -> Decimal:
    """高精度反正切，结果落在 (0, pi/2)（输入为正）。

    先用恒等式 ``atan(x) = pi/4 + atan((x-1)/(x+1))`` 把自变量收敛到
    ``[-1, 1]``，再用幂级数求值。
    """
    if value < 0:
        return -_atan(-value)
    reduced = (value - 1) / (value + 1)
    power = reduced
    reduced_squared = reduced * reduced
    total = reduced
    term_index = 1
    while True:
        power *= reduced_squared
        term_index += 2
        term = power / Decimal(term_index)
        total -= term
        if term.copy_abs() < Decimal(1).scaleb(-35):
            break
        power *= reduced_squared
        term_index += 2
        term = power / Decimal(term_index)
        total += term
        if term.copy_abs() < Decimal(1).scaleb(-35):
            break
    return _PI / Decimal(4) + total


def _quantize(value: Decimal, precision: int) -> Decimal:
    quantum = Decimal(1).scaleb(-precision)
    return value.quantize(quantum, rounding=ROUND_HALF_EVEN)


def normalize_coordinate(
    lon,
    lat,
    srid: int = EPSG_4326,
    precision: int = DEFAULT_PRECISION,
) -> tuple[Decimal, Decimal]:
    """把单个坐标点规范化为 EPSG:4326 的 ``(lon, lat)``。

    参数
    ------
    lon, lat:
        4326 下分别是经度、纬度；3857 下分别是东向、北向米制坐标。
    srid:
        输入坐标的 SRID，仅支持 4326 / 3857。
    precision:
        规范化后保留的固定小数位数，使用银行家舍入（ROUND_HALF_EVEN）。

    抛出
    ------
    InvalidCoordinateError:
        值非有限、无法解析，或经纬度超出合法范围。
    """
    if srid not in SUPPORTED_SRIDS:
        raise InvalidCoordinateError(f"不支持的 SRID: {srid!r}")
    if not isinstance(precision, int) or precision < 0:
        raise InvalidCoordinateError(f"非法精度: {precision!r}")

    first = _to_decimal(lon)
    second = _to_decimal(lat)

    if srid == EPSG_3857:
        first, second = _web_mercator_to_lonlat(first, second)

    if abs(first) > MAX_LON or abs(second) > MAX_LAT:
        raise InvalidCoordinateError(
            f"坐标越界: lon={first}, lat={second}（SRID {srid}）"
        )

    return _quantize(first, precision), _quantize(second, precision)


def quantum(precision: int = DEFAULT_PRECISION) -> Decimal:
    """返回给定精度对应的最小量化步长。"""
    return Decimal(1).scaleb(-precision)
