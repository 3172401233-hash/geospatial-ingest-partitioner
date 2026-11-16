# geospatial-ingest-partitioner

Python 地理事件接入分区库。把来自 **GeoJSON 与 CSV** 的车载事件统一规范化，
按固定经纬度网格生成**确定性分区键**，维护跨网格空间索引，并正确处理
重复事件、迟到更新与日期变更线（antimeridian）跨越。

纯标准库实现（`decimal` / `dataclasses` / `csv` / `json` / `unittest`），
无第三方依赖。

## 快速开始

```python
from decimal import Decimal

from geo_ingest import EventPartitionStore, Grid, parse_csv, parse_geojson

grid = Grid(Decimal("1"))          # 1° × 1° 固定网格
store = EventPartitionStore(grid)

events = parse_geojson(geojson_payload) + parse_csv(csv_text_lines)
for event in events:
    store.upsert(event)

store.partition_map()              # 物理分区 -> event_id 列表（已排序）
store.spatial_index()              # 网格键 -> 索引到的 event_id 列表（已排序）
store.query_cell("190:095")        # 按网格查询（含跨格触及的事件）
store.home_partition("veh-1")      # 事件唯一的物理归属分区

snapshot = store.to_snapshot()     # 确定性快照，可在另一进程重建
rebuilt = EventPartitionStore.from_snapshot(snapshot)
```

## 包结构

| 模块 | 职责 |
| --- | --- |
| `geo_ingest.crs` | SRID 处理、3857→4326 反投影、坐标量化与合法性校验 |
| `geo_ingest.geometry` | Point / LineString / Polygon 规范化、WKT 与 GeoJSON 解析、日期变更线解绕 |
| `geo_ingest.grid` | 固定网格、稳定分区键、半开区间边界、超覆盖（supercover）跨格算法 |
| `geo_ingest.event` | 事件模型、时间戳解析、版本令牌（更新时间 + 内容哈希） |
| `geo_ingest.partitioner` | 物理归属、空间索引、迟到覆盖、确定性快照与重建 |
| `geo_ingest.parsers` | GeoJSON FeatureCollection 与带表头 CSV 解析 |

## CRS 规则

- 仅支持两个 SRID：
  - `4326` —— WGS84 经纬度，经度 `[-180, 180]`、纬度 `[-90, 90]`；
  - `3857` —— Web Mercator 米制投影坐标，接入时反投影到 4326。
- 所有几何在构造时即规范化到 **EPSG:4326**，下游分区逻辑不区分来源文件。
- SRID 来源优先级：显式参数 `srid=` > 行内 `srid` / `crs` 列 >
  GeoJSON 旧式 `crs` 成员 > 默认 4326（RFC 7946）。
- 非法坐标一律拒绝并抛出 `InvalidCoordinateError`：
  超出经纬度范围、`NaN` / `Infinity`、无法解析的字符串、不支持的 SRID。
  注意经度 `±180`、纬度 `±90` 是**合法闭合边界**，不做自动 wrap。
- 3857 的反投影在 40 位 `Decimal` 精度下计算，再按目标精度量化，
  避免 `float` 二进制误差影响分区键。

## 精度规则

- 规范化坐标统一为 `Decimal`，默认保留 **7 位小数**（约 1.1 cm 量级），
  可通过 `precision=` 调整（含 `0`）。
- 舍入采用 **ROUND_HALF_EVEN（银行家舍入）**，结果可复现、跨平台一致。
- `float` 输入先经过 `str()` 再转 `Decimal`，因此 `10.1` 与 `"10.1"`
  产生完全相同的规范化值；`"10"`、`"10.0000000"` 也会塌缩为同一值。
- 网格索引全部基于量化后的 `Decimal` 运算（地板除使用显式
  `ROUND_FLOOR`，不依赖 `Decimal //` 的截断语义），相同坐标在任何机器、
  任何写入顺序下都得到相同的单元格键。

## 固定网格与边界归属

- 网格原点固定在 `(-180, -90)`，`cell_size` 为度数，必须同时整除 360 与 180
  （如 `1`、`0.5`、`0.25`），否则构造 `Grid` 时直接报错。
- 单元格键格式为零填充的 `"列:行"`（如 `"190:095"`），字典序与空间顺序一致。
- 边界采用**半开区间**：

  ```
  列 i 覆盖 [origin_lon + i*size, origin_lon + (i+1)*size)
  行 j 覆盖 [origin_lat + j*size, origin_lat + (j+1)*size)
  ```

  西/南边界属于本格，东/北边界属于相邻格；最东 `lon=180` 与最北 `lat=90`
  这两个闭合边界收敛到最后一个单元格，因此不会落到网格之外。
- 固定网格替代“按文件顺序写入的大分区”：热点区域内的点按坐标均匀分散到
  各自所在的固定单元格，分区大小只与单元格边长有关。

## 跨格几何：索引与物理归属分离

- **物理归属唯一**：每条记录只属于一个分区，键由几何锚点决定
  （Point 取自身，LineString / Polygon 取第一个坐标点）。
  `partition_map()` 中一个 `event_id` 只会出现一次。
- **空间索引可跨格**：LineString 用 Amanatides-Woo voxel traversal 的
  Decimal 精确实现求超覆盖；Polygon 取所有边的超覆盖加上内部覆盖到的
  单元格中心（内环/洞被排除）。几何触及的每个网格都能通过
  `query_cell()` 检索到该事件。
- 恰好穿过格点（corner）的线段会登记相邻的全部单元格，保证查询不丢点；
  遍历带迭代上限，异常不收敛时显式报错而不是死循环。
- 索引条目**不代表物理归属**：`partition_events(key)` 只返回真正落在该
  分区的事件，跨格事件不会被重复写入两个物理分区。

## 日期变更线

- 存储坐标保持规范化原值（如 `-179.9` 不会被改写）。
- 网格计算在“解绕经度”空间进行：相邻点经度差超过 180° 时按
  `±360°` 累计平移；闭合环的闭合点参与解绕判断，保证环在解绕空间仍闭合。
- 解绕得到的列号再按 360° **环绕折叠**（模 `width`）成合法键，
  因此跨越 `±180°` 的几何会同时索引到日期变更线两侧的相邻单元格，
  而物理锚点仍只有一个。两个方向（东→西、西→东）的跨越行为对称。

## 重复事件与迟到更新

- 以 `event_id` 为业务主键，存储中每个 ID 永远只有一个版本。
- 版本令牌为 `(updated_at, content_hash)` 的全序：
  - 先比较 `updated_at`（统一解析为带 UTC 时区的 `datetime`）；
  - 时间戳相同时用规范化内容的 SHA-256 哈希决胜，保证结果确定。
- `upsert()` 返回 `UpsertResult`：
  - `inserted` —— 新事件；
  - `updated` —— 更晚版本覆盖旧版本；
  - `stale` —— 旧版本迟到，拒绝；
  - `duplicate` —— 同版本重复写入，拒绝。
- 覆盖时若新版本的物理分区发生变化，会**先清理旧分区与全部旧索引条目**
  再按新几何重建，不会因分区计算变化而留下两份记录或悬空索引。
- 最终状态只取决于输入事件集合，与写入顺序、分区声明顺序无关。

## 快照与确定性重建

- `to_snapshot()` / `to_json()` 导出网格参数、按 `event_id` 排序的事件、
  排序后的分区映射与空间索引。
- `from_snapshot()` / `from_json()` 重新 upsert 并**校验**重建出的
  分区映射与索引和快照完全一致，同时校验内容哈希，任何篡改都会报错。
- 同一批数据无论按什么顺序写入、在什么进程重建，都会得到字节级相同的 JSON。

## 输入格式

- GeoJSON：`FeatureCollection`、单个 `Feature` 或裸 geometry；
  必填 `event_id`（别名 `id`）与 `updated_at`
  （别名 `timestamp` / `event_time` / `time`）。
- CSV：带表头，几何二选一：
  - `wkt` 列（POINT / LINESTRING / POLYGON，含逗号的 WKT 需按 CSV 规范加引号）；
  - `lon` + `lat` 列（别名 `longitude`/`lng`/`x`、`latitude`/`y`），生成 Point。
- CSV 行级错误会附带原始行号，便于定位脏数据。

## 运行测试

```bash
python -m unittest
```

测试覆盖：非法坐标、3857 反投影、精度与舍入、半开区间边界、
日期变更线双向跨越、重复事件、同时间戳决胜、迟到/乱序更新、
跨格物理归属唯一、分区迁移清理、声明顺序无关性以及快照重建。
