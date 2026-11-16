import unittest
from decimal import Decimal

from geo_ingest import EventPartitionStore, Grid, parse_csv, parse_geojson
from geo_ingest.event import Event
from geo_ingest.geometry import Geometry


class MixedSourceIngestionTest(unittest.TestCase):
    """GeoJSON 与 CSV 混合、重复、迟到、跨格的端到端场景。"""

    def setUp(self):
        self.grid = Grid(Decimal("1"))
        self.store = EventPartitionStore(self.grid)

        geojson = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [10.2, 20.4]},
                    "properties": {
                        "event_id": "veh-1",
                        "updated_at": "2026-01-01T00:00:00Z",
                        "source": "geojson",
                    },
                },
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [[9.5, 5], [10.5, 5]],
                    },
                    "properties": {
                        "event_id": "veh-2",
                        "updated_at": "2026-01-01T00:00:00Z",
                        "source": "geojson",
                    },
                },
            ],
        }
        self.csv_rows = [
            # 同事件迟到更新（来自 CSV，精度表示不同）
            "event_id,lon,lat,updated_at,source",
            "veh-1,10.2000000,20.4000000,2026-01-02T00:00:00Z,csv",
        ]
        self.geojson_events = parse_geojson(geojson)
        self.csv_events = parse_csv(self.csv_rows, srid=None)
        # 3857 坐标行，通过 srid 列声明 CRS，规范化后落到 lon=10
        self.csv_events_3857 = parse_csv(
            [
                "event_id,lon,lat,updated_at,srid,source",
                "veh-3,1113194.9079327357,0,2026-01-01T00:00:00Z,3857,csv",
            ]
        )

    def test_full_pipeline(self):
        results = self.store.ingest(
            self.geojson_events + self.csv_events + self.csv_events_3857
        )
        self.assertEqual(self.store.event_count, 3)
        accepted = [r.reason for r in results if r.accepted]
        self.assertIn("updated", accepted)

        # veh-1 迟到更新生效，且全局只有一份
        veh1 = self.store.get("veh-1")
        self.assertEqual(veh1.properties["source"], "csv")
        self.assertEqual(
            veh1.geometry.coordinates,
            (Decimal("10.2000000"), Decimal("20.4000000")),
        )

        # veh-2 跨格：索引两处、物理归属一处
        self.assertEqual(
            self.store.keys_for_event("veh-2"),
            frozenset({"189:095", "190:095"}),
        )
        self.assertEqual(self.store.home_partition("veh-2"), "189:095")

        # veh-3 的 3857 坐标规范化到 lon=10
        self.assertEqual(
            self.store.get("veh-3").geometry.coordinates[0],
            Decimal("10.0000000"),
        )

    def test_replay_in_any_order_rebuilds_same_snapshot(self):
        all_events = self.geojson_events + self.csv_events + self.csv_events_3857
        self.store.ingest(all_events)
        snapshot = self.store.to_snapshot()

        replayed = EventPartitionStore(self.grid)
        for event in reversed(all_events):
            replayed.upsert(event)
        self.assertEqual(replayed.to_snapshot(), snapshot)

        rebuilt = EventPartitionStore.from_snapshot(snapshot)
        self.assertEqual(rebuilt.partition_map(), self.store.partition_map())
        self.assertEqual(rebuilt.spatial_index(), self.store.spatial_index())

    def test_hotspot_is_split_into_fixed_cells(self):
        # 同一热点区域内大量点，应分散到多个固定网格而不是一个大分区
        events = [
            Event.create(
                f"hot-{i}",
                    Geometry("Point", [10 + (i % 5) * 0.5, 20 + (i // 5) * 0.5]),
                "2026-01-01T00:00:00Z",
            )
            for i in range(25)
        ]
        self.store.ingest(events)
        partitions = self.store.partition_map()
        self.assertGreater(len(partitions), 1)
        self.assertEqual(
            sum(len(ids) for ids in partitions.values()), 25
        )


if __name__ == "__main__":
    unittest.main()
