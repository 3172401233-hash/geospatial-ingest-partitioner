"""固定经纬度网格与确定性分区键。

网格原点固定在 ``(-180, -90)``，单元格边长为 ``cell_size`` 度，
横向把全球分成 ``360 / cell_size`` 列、纵向分成 ``180 / cell_size`` 行。

边界归属规则（半开区间）::

    列 i 覆盖 [origin_lon + i*size, origin_lon + (i+1)*size)
    行 j 覆盖 [origin_lat + j*size, origin_lat + (j+1)*size)

即西/南边界属于本格，东/北边界属于相邻格；最东、最北的闭合边界
    （lon=180、lat=90）收敛到最后一个单元格。所有运算使用 ``Decimal``，
不存在浮点取整歧义，相同坐标在任何机器、任何写入顺序下得到相同键。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .geometry import (
    LINESTRING,
    POINT,
    Geometry,
    LonLat,
    point_in_polygon,
)

ORIGIN_LON = Decimal("-180")
ORIGIN_LAT = Decimal("-90")
FULL_WORLD = Decimal("360")
HALF_WORLD = Decimal("180")


def _floor_div(numerator: Decimal, denominator: Decimal) -> int:
    """Decimal 地板除。

    ``Decimal // Decimal`` 默认按上下文精度截断且向零取整，与整数
    地板除语义不同，负坐标会算错列号，这里统一成真正的 floor。
    """
    quotient = numerator / denominator
    return int(quotient.to_integral_value(rounding="ROUND_FLOOR"))


@dataclass(frozen=True)
class Grid:
    """固定网格定义。``cell_size`` 必须能整除 360 和 180。"""

    cell_size: Decimal

    def __post_init__(self) -> None:
        size = self.cell_size
        if not isinstance(size, Decimal):
            raise ValueError("cell_size 必须是 Decimal")
        if size <= 0:
            raise ValueError("cell_size 必须为正数")
        if FULL_WORLD % size != 0 or HALF_WORLD % size != 0:
            raise ValueError("cell_size 必须同时整除 360 和 180")
        object.__setattr__(self, "cell_size", size)

    @property
    def width(self) -> int:
        return int(FULL_WORLD / self.cell_size)

    @property
    def height(self) -> int:
        return int(HALF_WORLD / self.cell_size)

    @property
    def _pad(self) -> int:
        return len(str(max(self.width, self.height) - 1))

    # ---- 索引 -----------------------------------------------------------

    def raw_index(self, lon: Decimal, lat: Decimal) -> tuple[int, int]:
        """解绕空间下的原始列/行号，列号可能超出 [0, width)。"""
        i = _floor_div(lon - ORIGIN_LON, self.cell_size)
        j = _floor_div(lat - ORIGIN_LAT, self.cell_size)
        return i, j

    def wrap(self, i: int, j: int) -> tuple[int, int]:
        """把解绕列号按 360° 环绕折叠进合法范围，并收敛闭合边界。"""
        i %= self.width
        if j == self.height:
            j = self.height - 1
        if not (0 <= j < self.height):
            raise ValueError(f"行号超出网格范围: {j}")
        return i, j

    def cell_key(self, i: int, j: int) -> str:
        """生成零填充、字典序与空间顺序一致的稳定分区键。"""
        i, j = self.wrap(i, j)
        return f"{i:0{self._pad}d}:{j:0{self._pad}d}"

    def key_for_point(self, lon: Decimal, lat: Decimal) -> str:
        """规范化经纬度点所在单元格的键。"""
        i, j = self.raw_index(lon, lat)
        if i == self.width:
            i = self.width - 1
        if j == self.height:
            j = self.height - 1
        return self.cell_key(i, j)

    def anchor_key(self, geometry: Geometry) -> str:
        """几何的唯一物理归属键（由锚点决定）。"""
        lon, lat = geometry.anchor()
        return self.key_for_point(lon, lat)

    # ---- 跨格覆盖 -------------------------------------------------------

    def coverage(self, geometry: Geometry) -> frozenset[str]:
        """几何触及的全部网格键（空间索引条目）。

        Point 只有一个键；LineString 使用 voxel traversal 求精确超覆盖；
        Polygon 取所有边的超覆盖加上内部覆盖到的单元格中心。
        物理锚点键始终包含在结果中。
        """
        if geometry.geom_type == POINT:
            return frozenset({self.anchor_key(geometry)})

        unwrapped = geometry.unwrapped_coordinates()
        raw_cells: set[tuple[int, int]] = set()

        if geometry.geom_type == LINESTRING:
            self._cover_ring(unwrapped, raw_cells)
        else:
            for ring in unwrapped:
                self._cover_ring(ring, raw_cells)
            self._cover_polygon_interior(unwrapped, raw_cells)

        keys = {self.cell_key(i, j) for i, j in raw_cells}
        keys.add(self.anchor_key(geometry))
        return frozenset(keys)

    def _cover_ring(self, ring: list[LonLat], out: set[tuple[int, int]]) -> None:
        for start, end in zip(ring, ring[1:]):
            out.update(self._supercover(start, end))

    def _supercover(
        self, start: LonLat, end: LonLat
    ) -> set[tuple[int, int]]:
        """Amanatides-Woo 体素遍历的 Decimal 精确实现。"""
        size = self.cell_size
        i, j = self.raw_index(*start)
        end_i, end_j = self.raw_index(*end)
        cells = {(i, j)}
        if (i, j) == (end_i, end_j):
            return cells

        dx = end[0] - start[0]
        dy = end[1] - start[1]
        step_x = 1 if dx > 0 else (-1 if dx < 0 else 0)
        step_y = 1 if dy > 0 else (-1 if dy < 0 else 0)

        t_max_x = self._boundary_t(start[0], dx, i, step_x, size, ORIGIN_LON)
        t_max_y = self._boundary_t(start[1], dy, j, step_y, size, ORIGIN_LAT)
        t_delta_x = size / abs(dx) if dx else None
        t_delta_y = size / abs(dy) if dy else None

        max_steps = 2 * (abs(end_i - i) + abs(end_j - j)) + 4
        iterations = 0
        while (i, j) != (end_i, end_j):
            iterations += 1
            if iterations > max_steps:
                raise RuntimeError(
                    f"supercover 遍历未收敛: {start} -> {end}"
                )
            if t_max_x is None:
                advance_x = False
            elif t_max_y is None:
                advance_x = True
            else:
                advance_x = t_max_x < t_max_y

            if advance_x:
                i += step_x
                t_max_x += t_delta_x
            elif (
                t_max_y is None
                or t_max_x is None
                or t_max_y < t_max_x
            ):
                j += step_y
                t_max_y += t_delta_y
            else:
                # 恰好穿过格点：按查询完整性把相邻三格全部登记
                cells.add((i + step_x, j))
                cells.add((i, j + step_y))
                i += step_x
                j += step_y
                t_max_x += t_delta_x
                t_max_y += t_delta_y
            cells.add((i, j))
        return cells

    @staticmethod
    def _boundary_t(
        coordinate: Decimal,
        delta: Decimal,
        index: int,
        step: int,
        size: Decimal,
        origin: Decimal,
    ):
        if step == 0:
            return None
        boundary_index = index + 1 if step > 0 else index
        boundary = origin + boundary_index * size
        return (boundary - coordinate) / delta

    def _cover_polygon_interior(
        self,
        rings: list[list[LonLat]],
        out: set[tuple[int, int]],
    ) -> None:
        min_lon, min_lat, max_lon, max_lat = self._rings_bbox(rings)
        min_i, min_j = self.raw_index(min_lon, min_lat)
        max_i, max_j = self.raw_index(max_lon, max_lat)
        size = self.cell_size
        for i in range(min_i, max_i + 1):
            center_lon = ORIGIN_LON + (Decimal(i) + Decimal("0.5")) * size
            for j in range(min_j, max_j + 1):
                center_lat = ORIGIN_LAT + (Decimal(j) + Decimal("0.5")) * size
                if point_in_polygon((center_lon, center_lat), rings):
                    out.add((i, j))

    @staticmethod
    def _rings_bbox(rings):
        lons = [p[0] for ring in rings for p in ring]
        lats = [p[1] for ring in rings for p in ring]
        return min(lons), min(lats), max(lons), max(lats)
