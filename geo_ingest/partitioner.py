"""事件分区存储：物理归属唯一、空间索引可跨格、迟到更新可覆盖。

三类状态彼此独立维护：

* ``_events``      —— ``event_id -> Event``，每个事件只有一个版本；
* ``_home``        —— ``event_id -> 物理分区键``，一条记录只归属一个分区；
* ``_index``       —— ``网格键 -> {event_id}``，跨格几何在相邻格中可被检索到，
  但索引条目不代表物理归属。

最终状态只取决于输入事件集合，与写入顺序、分区声明顺序无关：
按 ``event_id`` 排序导出的快照可在另一进程中重建出完全相同的分区映射与索引。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from .event import Event
from .geometry import Geometry
from .grid import Grid


@dataclass(frozen=True)
class UpsertResult:
    """单次写入结果。"""

    event_id: str
    accepted: bool
    reason: str
    home_key: str
    indexed_keys: frozenset[str]
    previous_home_key: str | None = None


class EventPartitionStore:
    """固定网格上的事件分区存储。"""

    def __init__(self, grid: Grid) -> None:
        if not isinstance(grid, Grid):
            raise TypeError("grid 必须是 Grid 实例")
        self.grid = grid
        self._events: dict[str, Event] = {}
        self._home: dict[str, str] = {}
        self._index: dict[str, set[str]] = {}

    # ---- 写入 -----------------------------------------------------------

    def upsert(self, event: Event) -> UpsertResult:
        """写入或覆盖一个事件。

        旧版本（或同版本重复写入）不会产生第二条记录；若新版本的物理分区
        发生变化，旧分区与旧索引条目会先被清理，再按新几何重建。
        """
        if not isinstance(event, Event):
            raise TypeError("event 必须是 Event 实例")

        existing = self._events.get(event.event_id)
        if existing is not None and not event.is_newer_than(existing):
            reason = (
                "stale"
                if event.version_token() < existing.version_token()
                else "duplicate"
            )
            return UpsertResult(
                event_id=event.event_id,
                accepted=False,
                reason=reason,
                home_key=self._home[event.event_id],
                indexed_keys=self.keys_for_event(event.event_id),
            )

        previous_home = self._home.get(event.event_id)
        if existing is not None:
            self._remove_index_entries(event.event_id)

        home_key = self.grid.anchor_key(event.geometry)
        coverage = self.grid.coverage(event.geometry)

        self._events[event.event_id] = event
        self._home[event.event_id] = home_key
        for key in coverage:
            self._index.setdefault(key, set()).add(event.event_id)

        return UpsertResult(
            event_id=event.event_id,
            accepted=True,
            reason="inserted" if existing is None else "updated",
            home_key=home_key,
            indexed_keys=coverage,
            previous_home_key=previous_home,
        )

    def ingest(self, events: Iterable[Event]) -> list[UpsertResult]:
        """批量写入，返回与输入等长、同序的写入结果。"""
        return [self.upsert(event) for event in events]

    def _remove_index_entries(self, event_id: str) -> None:
        empty_keys = []
        for key, members in self._index.items():
            members.discard(event_id)
            if not members:
                empty_keys.append(key)
        for key in empty_keys:
            del self._index[key]

    # ---- 读取 -----------------------------------------------------------

    def get(self, event_id: str) -> Event | None:
        return self._events.get(event_id)

    def home_partition(self, event_id: str) -> str | None:
        """返回事件的唯一物理分区键。"""
        return self._home.get(event_id)

    def keys_for_event(self, event_id: str) -> frozenset[str]:
        """事件出现在哪些网格索引中（跨格几何可能有多个）。"""
        return frozenset(
            key for key, members in self._index.items() if event_id in members
        )

    def query_cell(self, cell_key: str) -> frozenset[str]:
        """查询落在某网格（含跨格触及）内的全部事件 ID。"""
        return frozenset(self._index.get(cell_key, ()))

    def query_point(self, lon: Decimal, lat: Decimal) -> frozenset[str]:
        """按经纬度点定位网格并查询。"""
        return self.query_cell(self.grid.key_for_point(lon, lat))

    def partition_events(self, cell_key: str) -> dict[str, Event]:
        """物理归属在该分区的事件（跨格索引事件不计入物理归属）。"""
        return {
            event_id: event
            for event_id, event in self._events.items()
            if self._home.get(event_id) == cell_key
        }

    @property
    def event_count(self) -> int:
        return len(self._events)

    def partition_map(self) -> dict[str, list[str]]:
        """物理分区 -> 事件 ID 列表，键与 ID 全部排序，确定性输出。"""
        result: dict[str, list[str]] = {}
        for event_id, key in self._home.items():
            result.setdefault(key, []).append(event_id)
        return {key: sorted(ids) for key, ids in sorted(result.items())}

    def spatial_index(self) -> dict[str, list[str]]:
        """网格键 -> 索引到的事件 ID 列表，键与 ID 全部排序。"""
        return {
            key: sorted(members)
            for key, members in sorted(self._index.items())
            if members
        }

    # ---- 快照与重建 -----------------------------------------------------

    def to_snapshot(self) -> dict:
        """导出与写入顺序无关的确定性快照。"""
        return {
            "grid": {"cell_size": str(self.grid.cell_size)},
            "partition_map": self.partition_map(),
            "spatial_index": self.spatial_index(),
            "events": [
                self._events[event_id].to_record()
                for event_id in sorted(self._events)
            ],
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_snapshot(),
            ensure_ascii=False,
            sort_keys=False,
            separators=(",", ":"),
            cls=_SnapshotEncoder,
        )

    @classmethod
    def from_snapshot(cls, snapshot: dict) -> "EventPartitionStore":
        """从快照重建存储，并校验分区映射与索引与快照完全一致。"""
        grid = Grid(Decimal(str(snapshot["grid"]["cell_size"])))
        store = cls(grid)
        records = sorted(snapshot["events"], key=lambda item: item["event_id"])
        for record in records:
            event = _event_from_record(record)
            store.upsert(event)

        if store.partition_map() != snapshot["partition_map"]:
            raise ValueError("快照重建失败：分区映射不一致")
        if store.spatial_index() != snapshot["spatial_index"]:
            raise ValueError("快照重建失败：空间索引不一致")
        return store

    @classmethod
    def from_json(cls, text: str) -> "EventPartitionStore":
        return cls.from_snapshot(json.loads(text))


class _SnapshotEncoder(json.JSONEncoder):
    def default(self, value):
        if isinstance(value, Decimal):
            return str(value)
        return super().default(value)


def _event_from_record(record: dict) -> Event:
    geometry = Geometry.from_geojson(record["geometry"])
    event = Event.create(
        event_id=record["event_id"],
        geometry=geometry,
        updated_at=record["updated_at"],
        properties=record.get("properties", {}),
    )
    stored_hash = record.get("content_hash")
    if stored_hash and stored_hash != event.content_hash:
        raise ValueError(
            f"事件 {event.event_id} 内容哈希与快照不一致，数据已损坏"
        )
    return event
