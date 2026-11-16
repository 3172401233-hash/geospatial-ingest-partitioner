import unittest
from datetime import datetime, timezone
from decimal import Decimal

from geo_ingest.event import Event
from geo_ingest.geometry import LINESTRING, POINT, POLYGON, Geometry
from geo_ingest.grid import Grid
from geo_ingest.partitioner import EventPartitionStore


def point_event(event_id, lon, lat, updated_at, properties=None):
    return Event.create(
        event_id,
        Geometry(POINT, [lon, lat]),
        updated_at,
        properties or {},
    )


class EventVersionTest(unittest.TestCase):
    def test_timestamp_parsing_normalizes_to_utc(self):
        event = point_event("e1", 0, 0, "2026-01-01T08:00:00+08:00")
        self.assertEqual(
            event.updated_at,
            datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
        )

    def test_naive_timestamp_assumed_utc(self):
        event = point_event("e1", 0, 0, "2026-01-01T00:00:00")
        self.assertEqual(event.updated_at.utcoffset(), timezone.utc.utcoffset(None))

    def test_epoch_timestamp(self):
        event = point_event("e1", 0, 0, 1767225600)
        self.assertEqual(event.updated_at.year, 2026)

    def test_content_hash_breaks_same_timestamp_tie(self):
        event_a = point_event("e1", 0, 0, "2026-01-01T00:00:00Z", {"v": 1})
        event_b = point_event("e1", 1, 1, "2026-01-01T00:00:00Z", {"v": 2})
        winner, loser = (
            (event_b, event_a)
            if event_b.content_hash > event_a.content_hash
            else (event_a, event_b)
        )
        self.assertTrue(winner.is_newer_than(loser))
        self.assertFalse(loser.is_newer_than(winner))

    def test_identical_events_have_equal_version(self):
        a = point_event("e1", 0, 0, "2026-01-01T00:00:00Z", {"v": 1})
        b = point_event("e1", 0, 0, "2026-01-01T00:00:00Z", {"v": 1})
        self.assertEqual(a.version_token(), b.version_token())


class PartitionerUpsertTest(unittest.TestCase):
    def setUp(self):
        self.grid = Grid(Decimal("1"))
        self.store = EventPartitionStore(self.grid)

    def test_insert_then_duplicate_is_rejected(self):
        event = point_event("e1", 10.2, 20.4, "2026-01-01T00:00:00Z")
        first = self.store.upsert(event)
        second = self.store.upsert(point_event("e1", 10.2, 20.4,
                                               "2026-01-01T00:00:00Z"))
        self.assertTrue(first.accepted)
        self.assertFalse(second.accepted)
        self.assertEqual(second.reason, "duplicate")
        self.assertEqual(self.store.event_count, 1)

    def test_late_update_overwrites_old_version(self):
        self.store.upsert(point_event("e1", 10.2, 20.4,
                                      "2026-01-01T00:00:00Z", {"v": 1}))
        result = self.store.upsert(
            point_event("e1", 10.2, 20.4, "2026-01-02T00:00:00Z", {"v": 2})
        )
        self.assertTrue(result.accepted)
        self.assertEqual(result.reason, "updated")
        self.assertEqual(self.store.event_count, 1)
        self.assertEqual(self.store.get("e1").properties, {"v": 2})

    def test_stale_update_is_ignored(self):
        self.store.upsert(point_event("e1", 10.2, 20.4,
                                      "2026-01-02T00:00:00Z", {"v": 2}))
        stale = self.store.upsert(
            point_event("e1", 5, 5, "2026-01-01T00:00:00Z", {"v": 0})
        )
        self.assertFalse(stale.accepted)
        self.assertEqual(stale.reason, "stale")
        self.assertEqual(self.store.get("e1").properties, {"v": 2})

    def test_out_of_order_arrival_converges(self):
        # 新版本先到、旧版本后到，最终仍只保留新版本
        self.store.upsert(point_event("e1", 10, 10, "2026-01-03T00:00:00Z"))
        self.store.upsert(point_event("e1", 10, 10, "2026-01-02T00:00:00Z"))
        self.assertEqual(
            self.store.get("e1").updated_at.isoformat(),
            "2026-01-03T00:00:00+00:00",
        )


class HomePartitionUniquenessTest(unittest.TestCase):
    def setUp(self):
        self.grid = Grid(Decimal("1"))
        self.store = EventPartitionStore(self.grid)

    def test_crossing_line_has_one_home_but_multiple_index_cells(self):
        line = Geometry(LINESTRING, [[9.5, 5], [10.5, 5]])
        event = Event.create("c1", line, "2026-01-01T00:00:00Z")
        result = self.store.upsert(event)
        self.assertEqual(result.home_key, "189:095")
        self.assertEqual(
            result.indexed_keys, frozenset({"189:095", "190:095"})
        )
        # 物理归属只有一个分区
        homes = [
            key for key, ids in self.store.partition_map().items() if "c1" in ids
        ]
        self.assertEqual(homes, ["189:095"])
        # 相邻格通过索引能查到，但物理分区里没有这条记录
        self.assertEqual(self.store.query_cell("190:095"), frozenset({"c1"}))
        self.assertEqual(self.store.partition_events("190:095"), {})
        self.assertEqual(
            set(self.store.partition_events("189:095")), {"c1"}
        )

    def test_dateline_geometry_home_does_not_duplicate(self):
        geom = Geometry(POLYGON, [
            [[179, 0], [-179, 0], [-179, 2], [179, 2], [179, 0]]
        ])
        self.store.upsert(Event.create("d1", geom, "2026-01-01T00:00:00Z"))
        homes = [
            key for key, ids in self.store.partition_map().items() if "d1" in ids
        ]
        self.assertEqual(len(homes), 1)
        self.assertEqual(homes[0], self.store.home_partition("d1"))


class PartitionMigrationTest(unittest.TestCase):
    def setUp(self):
        self.grid = Grid(Decimal("1"))
        self.store = EventPartitionStore(self.grid)

    def test_update_moving_event_clears_old_partition_and_index(self):
        self.store.upsert(point_event("m1", 10.2, 20.4,
                                      "2026-01-01T00:00:00Z"))
        old_home = self.store.home_partition("m1")
        old_index = self.store.keys_for_event("m1")

        self.store.upsert(
            Event.create(
                "m1",
                Geometry(LINESTRING, [[-5, -5], [-4, -5]]),
                "2026-01-02T00:00:00Z",
            )
        )
        # 旧物理分区不再持有该事件
        self.assertNotIn("m1", self.store.partition_events(old_home))
        # 旧索引条目全部清空
        for key in old_index:
            self.assertNotIn("m1", self.store.query_cell(key))
        self.assertEqual(self.store.event_count, 1)
        self.assertEqual(self.store.home_partition("m1"), "175:085")
        self.assertEqual(
            self.store.keys_for_event("m1"),
            frozenset({"175:085", "176:085"}),
        )

    def test_index_cells_are_removed_when_they_become_empty(self):
        self.store.upsert(
            Event.create(
                "m2",
                Geometry(LINESTRING, [[9.5, 5], [10.5, 5]]),
                "2026-01-01T00:00:00Z",
            )
        )
        self.store.upsert(point_event("m2", 50, 50, "2026-01-02T00:00:00Z"))
        index = self.store.spatial_index()
        self.assertNotIn("189:095", index)
        self.assertNotIn("190:095", index)


class DeclarationOrderTest(unittest.TestCase):
    def setUp(self):
        self.grid = Grid(Decimal("1"))
        self.events = [
            point_event("e1", 1.1, 1.1, "2026-01-01T00:00:00Z"),
            Event.create(
                "e2",
                Geometry(LINESTRING, [[9.5, 5], [10.5, 5]]),
                "2026-01-02T00:00:00Z",
            ),
            point_event("e1", 2.2, 2.2, "2026-01-03T00:00:00Z", {"new": True}),
            point_event("e3", 50, 50, "2026-01-01T00:00:00Z"),
            point_event("e2", 50, 50, "2026-01-04T00:00:00Z"),
        ]

    def _build(self, order):
        store = EventPartitionStore(self.grid)
        for index in order:
            store.upsert(self.events[index])
        return store

    def test_partition_and_index_independent_of_order(self):
        orders = [
            (0, 1, 2, 3, 4),
            (4, 3, 2, 1, 0),
            (2, 0, 4, 1, 3),
            (3, 1, 4, 2, 0),
        ]
        baseline = self._build(orders[0])
        for order in orders[1:]:
            store = self._build(order)
            self.assertEqual(store.partition_map(), baseline.partition_map())
            self.assertEqual(store.spatial_index(), baseline.spatial_index())
            self.assertEqual(store.to_json(), baseline.to_json())

    def test_final_versions_match_regardless_of_order(self):
        store = self._build((4, 3, 2, 1, 0))
        self.assertEqual(store.get("e1").properties, {"new": True})
        self.assertEqual(store.get("e2").geometry.geom_type, POINT)


class SnapshotTest(unittest.TestCase):
    def setUp(self):
        self.grid = Grid(Decimal("1"))
        self.store = EventPartitionStore(self.grid)
        self.store.ingest([
            point_event("e1", 10.2, 20.4, "2026-01-01T00:00:00Z"),
            Event.create(
                "e2",
                Geometry(LINESTRING, [[9.5, 5], [10.5, 5]]),
                "2026-01-02T00:00:00Z",
            ),
            point_event("e3", -179.9, 1.5, "2026-01-01T00:00:00Z"),
        ])

    def test_snapshot_rebuilds_identical_mapping_and_index(self):
        snapshot = self.store.to_snapshot()
        rebuilt = EventPartitionStore.from_snapshot(snapshot)
        self.assertEqual(rebuilt.partition_map(), self.store.partition_map())
        self.assertEqual(rebuilt.spatial_index(), self.store.spatial_index())
        self.assertEqual(rebuilt.event_count, self.store.event_count)

    def test_json_roundtrip(self):
        rebuilt = EventPartitionStore.from_json(self.store.to_json())
        self.assertEqual(rebuilt.to_json(), self.store.to_json())
        self.assertEqual(
            rebuilt.get("e1").geometry.coordinates,
            self.store.get("e1").geometry.coordinates,
        )

    def test_snapshot_is_order_insensitive(self):
        other = EventPartitionStore(self.grid)
        for event in reversed(list(self.store._events.values())):
            other.upsert(event)
        self.assertEqual(other.to_snapshot(), self.store.to_snapshot())


if __name__ == "__main__":
    unittest.main()
