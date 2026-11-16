import unittest
from decimal import Decimal

from geo_ingest.crs import EPSG_3857
from geo_ingest.errors import InvalidGeometryError
from geo_ingest.geometry import (
    LINESTRING,
    POINT,
    POLYGON,
    Geometry,
    point_in_polygon,
)


class GeometryConstructionTest(unittest.TestCase):
    def test_point_normalizes_coordinates(self):
        geom = Geometry(POINT, ["10.123456789", 20])
        self.assertEqual(geom.coordinates, (Decimal("10.1234568"), Decimal("20.0000000")))

    def test_rejects_unknown_type(self):
        with self.assertRaises(InvalidGeometryError):
            Geometry("MultiPoint", [[0, 0]])

    def test_linestring_needs_two_points(self):
        with self.assertRaises(InvalidGeometryError):
            Geometry(LINESTRING, [[0, 0]])

    def test_polygon_ring_must_close(self):
        with self.assertRaises(InvalidGeometryError):
            Geometry(POLYGON, [[[0, 0], [1, 0], [1, 1], [0, 1]]])

    def test_polygon_ring_needs_four_positions(self):
        with self.assertRaises(InvalidGeometryError):
            Geometry(POLYGON, [[[0, 0], [1, 0], [0, 0]]])

    def test_propagates_invalid_coordinate(self):
        with self.assertRaises(Exception):
            Geometry(POINT, [999, 0])


class WktTest(unittest.TestCase):
    def test_parse_point_wkt(self):
        geom = Geometry.from_wkt("POINT (10.5 20.25)")
        self.assertEqual(geom.geom_type, POINT)
        self.assertEqual(geom.anchor(), (Decimal("10.5000000"), Decimal("20.2500000")))

    def test_parse_linestring_wkt(self):
        geom = Geometry.from_wkt("LINESTRING (0 0, 1 1, 2 2)")
        self.assertEqual(len(geom.coordinates), 3)

    def test_parse_polygon_wkt(self):
        wkt = "POLYGON ((0 0, 2 0, 2 2, 0 2, 0 0), (0.5 0.5, 1.5 0.5, 1.5 1.5, 0.5 1.5, 0.5 0.5))"
        geom = Geometry.from_wkt(wkt)
        self.assertEqual(geom.geom_type, POLYGON)
        self.assertEqual(len(geom.coordinates), 2)

    def test_wkt_roundtrip(self):
        wkt = "POLYGON ((0 0, 2 0, 2 2, 0 2, 0 0))"
        canonical = (
            "POLYGON ((0.0000000 0.0000000, 2.0000000 0.0000000, "
            "2.0000000 2.0000000, 0.0000000 2.0000000, 0.0000000 0.0000000))"
        )
        self.assertEqual(Geometry.from_wkt(wkt).to_wkt(), canonical)

    def test_bad_wkt(self):
        with self.assertRaises(InvalidGeometryError):
            Geometry.from_wkt("NOTAGEOM (1 2)")


class GeoJsonTest(unittest.TestCase):
    def test_from_and_to_geojson(self):
        obj = {"type": "Point", "coordinates": [10, 20]}
        geom = Geometry.from_geojson(obj)
        self.assertEqual(
            geom.to_geojson(),
            {"type": "Point", "coordinates": ["10.0000000", "20.0000000"]},
        )

    def test_geojson_3857_via_constructor(self):
        geom = Geometry.from_geojson(
            {"type": "Point", "coordinates": [1113194.9079327357, 2273030.926987689]},
            srid=EPSG_3857,
        )
        self.assertEqual(
            geom.coordinates, (Decimal("10.0000000"), Decimal("20.0000000"))
        )


class AntimeridianTest(unittest.TestCase):
    def test_unwrap_east_to_west_crossing(self):
        geom = Geometry(LINESTRING, [[179.9, 0], [-179.9, 0]])
        unwrapped = geom.unwrapped_coordinates()
        self.assertEqual(unwrapped[0][0], Decimal("179.9000000"))
        self.assertEqual(unwrapped[1][0], Decimal("180.1000000"))

    def test_unwrap_west_to_east_crossing(self):
        geom = Geometry(LINESTRING, [[-179.9, 0], [179.9, 0]])
        unwrapped = geom.unwrapped_coordinates()
        self.assertEqual(unwrapped[1][0], Decimal("-180.1000000"))

    def test_normal_line_is_not_shifted(self):
        geom = Geometry(LINESTRING, [[10, 0], [12, 0]])
        self.assertEqual(
            [p[0] for p in geom.unwrapped_coordinates()],
            [Decimal("10.0000000"), Decimal("12.0000000")],
        )

    def test_stored_coordinates_remain_canonical(self):
        geom = Geometry(LINESTRING, [[179.9, 0], [-179.9, 0]])
        self.assertEqual(geom.coordinates[1][0], Decimal("-179.9000000"))

    def test_closed_ring_unwrap_keeps_closing_point_aligned(self):
        geom = Geometry(
            POLYGON,
            [[[179, 0], [-179, 0], [-179, 2], [179, 2], [179, 0]]],
        )
        ring = geom.unwrapped_coordinates()[0]
        self.assertEqual(ring[0], ring[-1])
        self.assertTrue(all(179 <= lon <= 181 for lon, _ in ring))

    def test_repeated_dateline_crossing_accumulates_shift(self):
        geom = Geometry(LINESTRING, [[170, 0], [-170, 0], [170, 0]])
        lons = [point[0] for point in geom.unwrapped_coordinates()]
        # 折线不闭合：每次跨越按相邻解绕点独立平移
        self.assertEqual(lons, [Decimal("170"), Decimal("190"), Decimal("170")])


class PointInPolygonTest(unittest.TestCase):
    def setUp(self):
        self.square = [
            [(Decimal(0), Decimal(0)), (Decimal(4), Decimal(0)),
             (Decimal(4), Decimal(4)), (Decimal(0), Decimal(4)),
             (Decimal(0), Decimal(0))]
        ]

    def test_inside_and_outside(self):
        self.assertTrue(point_in_polygon((Decimal(2), Decimal(2)), self.square))
        self.assertFalse(point_in_polygon((Decimal(5), Decimal(2)), self.square))

    def test_hole_excludes_interior(self):
        rings = self.square + [
            [(Decimal(1), Decimal(1)), (Decimal(3), Decimal(1)),
             (Decimal(3), Decimal(3)), (Decimal(1), Decimal(3)),
             (Decimal(1), Decimal(1))]
        ]
        self.assertFalse(point_in_polygon((Decimal(2), Decimal(2)), rings))
        self.assertTrue(point_in_polygon((Decimal("0.5"), Decimal(2)), rings))


if __name__ == "__main__":
    unittest.main()
