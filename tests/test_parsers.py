import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from geo_ingest.crs import EPSG_3857
from geo_ingest.errors import InvalidCoordinateError, InvalidEventError, InvalidGeometryError
from geo_ingest.geometry import LINESTRING, POINT
from geo_ingest.parsers import parse_csv, parse_csv_file, parse_geojson, parse_geojson_file


class GeoJsonParserTest(unittest.TestCase):
    def test_feature_collection(self):
        payload = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [10, 20]},
                    "properties": {
                        "event_id": "g1",
                        "updated_at": "2026-01-01T00:00:00Z",
                        "speed": 42.5,
                    },
                }
            ],
        }
        events = parse_geojson(payload)
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.event_id, "g1")
        self.assertEqual(event.properties, {"speed": 42.5})
        self.assertEqual(
            event.geometry.coordinates,
            (Decimal("10.0000000"), Decimal("20.0000000")),
        )

    def test_single_feature_and_bare_geometry_need_id(self):
        feature = {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [1, 2]},
            "properties": {"event_id": "solo", "updated_at": "2026-01-01T00:00:00Z"},
        }
        self.assertEqual(parse_geojson(feature)[0].event_id, "solo")
        with self.assertRaises(InvalidEventError):
            parse_geojson({"type": "Point", "coordinates": [1, 2]})

    def test_legacy_crs_member_3857(self):
        payload = {
            "type": "FeatureCollection",
            "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::3857"}},
            "features": [
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Point",
                        "coordinates": [1113194.9079327357, 0],
                    },
                    "properties": {
                        "event_id": "m1",
                        "updated_at": "2026-01-01T00:00:00Z",
                    },
                }
            ],
        }
        events = parse_geojson(payload)
        self.assertEqual(events[0].geometry.srid, EPSG_3857)
        self.assertEqual(events[0].geometry.coordinates[0], Decimal("10.0000000"))

    def test_unsupported_crs_declaration(self):
        payload = {
            "type": "FeatureCollection",
            "crs": {"type": "name", "properties": {"name": "EPSG:4490"}},
            "features": [],
        }
        with self.assertRaises(InvalidGeometryError):
            parse_geojson(payload)

    def test_missing_required_fields(self):
        payload = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [1, 2]},
                    "properties": {"updated_at": "2026-01-01T00:00:00Z"},
                }
            ],
        }
        with self.assertRaises(InvalidEventError):
            parse_geojson(payload)


class CsvParserTest(unittest.TestCase):
    def test_lon_lat_columns(self):
        rows = [
            "event_id,lon,lat,updated_at,speed",
            "c1,10.5,20.5,2026-01-01T00:00:00Z,88",
        ]
        events = parse_csv(rows)
        self.assertEqual(events[0].event_id, "c1")
        self.assertEqual(
            events[0].geometry.coordinates,
            (Decimal("10.5000000"), Decimal("20.5000000")),
        )
        self.assertEqual(events[0].properties, {"speed": "88"})

    def test_wkt_column_supports_linestring(self):
        rows = [
            "event_id,wkt,updated_at",
            'w1,"LINESTRING (9.5 5, 10.5 5)",2026-01-01T00:00:00Z',
        ]
        events = parse_csv(rows)
        self.assertEqual(events[0].geometry.geom_type, LINESTRING)
        self.assertEqual(len(events[0].geometry.coordinates), 2)

    def test_column_aliases_and_explicit_srid(self):
        rows = [
            "id,longitude,latitude,timestamp",
            "c2,1113194.9079327357,0,2026-01-01T00:00:00Z",
        ]
        events = parse_csv(rows, srid=EPSG_3857)
        self.assertEqual(events[0].geometry.coordinates[0], Decimal("10.0000000"))
        self.assertEqual(events[0].geometry.geom_type, POINT)

    def test_row_level_error_references_line_number(self):
        rows = [
            "event_id,lon,lat,updated_at",
            "c3,200,0,2026-01-01T00:00:00Z",
        ]
        with self.assertRaises(InvalidCoordinateError) as ctx:
            parse_csv(rows)
        self.assertIn("第 2 行", str(ctx.exception))

    def test_missing_geometry_columns(self):
        rows = ["event_id,updated_at", "c4,2026-01-01T00:00:00Z"]
        with self.assertRaises(InvalidEventError):
            parse_csv(rows)


class FileParserTest(unittest.TestCase):
    def test_geojson_and_csv_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            geojson_path = Path(tmp) / "events.geojson"
            geojson_path.write_text(
                json.dumps({
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "geometry": {"type": "Point", "coordinates": [10, 20]},
                            "properties": {
                                "event_id": "f1",
                                "updated_at": "2026-01-01T00:00:00Z",
                            },
                        }
                    ],
                }),
                encoding="utf-8",
            )
            csv_path = Path(tmp) / "events.csv"
            csv_path.write_text(
                "event_id,lon,lat,updated_at\n"
                "f2,30,40,2026-01-02T00:00:00Z\n",
                encoding="utf-8",
            )
            geo_events = parse_geojson_file(geojson_path)
            csv_events = parse_csv_file(csv_path)
            self.assertEqual([e.event_id for e in geo_events + csv_events],
                             ["f1", "f2"])


if __name__ == "__main__":
    unittest.main()
