import unittest
from decimal import Decimal

from geo_ingest.geometry import LINESTRING, POINT, POLYGON, Geometry
from geo_ingest.grid import Grid


class GridConstructionTest(unittest.TestCase):
    def test_dimensions(self):
        grid = Grid(Decimal("1"))
        self.assertEqual((grid.width, grid.height), (360, 180))
        half = Grid(Decimal("0.5"))
        self.assertEqual((half.width, half.height), (720, 360))

    def test_reject_non_divisor_size(self):
        with self.assertRaises(ValueError):
            Grid(Decimal("0.7"))
        with self.assertRaises(ValueError):
            Grid(Decimal("0"))

    def test_reject_non_decimal_size(self):
        with self.assertRaises(ValueError):
            Grid(1)


class BoundaryOwnershipTest(unittest.TestCase):
    def setUp(self):
        self.grid = Grid(Decimal("1"))

    def test_half_open_interval(self):
        west = self.grid.key_for_point(Decimal("10.0"), Decimal("20"))
        east = self.grid.key_for_point(Decimal("9.9999999"), Decimal("20"))
        self.assertNotEqual(west, east)
        self.assertEqual(west, "190:110")
        self.assertEqual(east, "189:110")

    def test_closed_eastern_and_northern_edges(self):
        self.assertEqual(
            self.grid.key_for_point(Decimal("180"), Decimal("0")),
            self.grid.key_for_point(Decimal("179.9999999"), Decimal("0")),
        )
        self.assertEqual(
            self.grid.key_for_point(Decimal("0"), Decimal("90")),
            self.grid.key_for_point(Decimal("0"), Decimal("89.9999999")),
        )

    def test_origin_cell(self):
        self.assertEqual(
            self.grid.key_for_point(Decimal("-180"), Decimal("-90")), "000:000"
        )


class CoverageTest(unittest.TestCase):
    def setUp(self):
        self.grid = Grid(Decimal("1"))

    def test_point_coverage_is_single_key(self):
        geom = Geometry(POINT, [10.5, 20.5])
        self.assertEqual(
            self.grid.coverage(geom), frozenset({self.grid.anchor_key(geom)})
        )

    def test_line_crossing_one_boundary(self):
        geom = Geometry(LINESTRING, [[9.5, 5], [10.5, 5]])
        self.assertEqual(
            self.grid.coverage(geom),
            frozenset({"189:095", "190:095"}),
        )

    def test_line_ending_exactly_on_boundary(self):
        geom = Geometry(LINESTRING, [[9.5, 5], [10.0, 5]])
        self.assertEqual(
            self.grid.coverage(geom),
            frozenset({"189:095", "190:095"}),
        )

    def test_line_starting_exactly_on_boundary(self):
        geom = Geometry(LINESTRING, [[10.0, 5], [10.5, 5]])
        self.assertEqual(
            self.grid.coverage(geom), frozenset({"190:095"})
        )

    def test_diagonal_through_grid_corner_covers_four_cells(self):
        geom = Geometry(LINESTRING, [[9.5, 9.5], [10.5, 10.5]])
        self.assertEqual(
            self.grid.coverage(geom),
            frozenset({"189:099", "189:100", "190:099", "190:100"}),
        )

    def test_horizontal_line_covers_all_intermediate_cells(self):
        geom = Geometry(LINESTRING, [[-0.5, 0], [2.5, 0]])
        self.assertEqual(
            self.grid.coverage(geom),
            frozenset({"179:090", "180:090", "181:090", "182:090"}),
        )

    def test_coverage_always_contains_anchor_key(self):
        geom = Geometry(LINESTRING, [[9.5, 5], [10.5, 5]])
        self.assertIn(self.grid.anchor_key(geom), self.grid.coverage(geom))


class PolygonCoverageTest(unittest.TestCase):
    def setUp(self):
        self.grid = Grid(Decimal("1"))

    def test_unit_square_covers_four_cells(self):
        geom = Geometry(POLYGON, [[[10, 10], [11, 10], [11, 11],
                                   [10, 11], [10, 10]]])
        self.assertEqual(
            self.grid.coverage(geom),
            frozenset({"190:100", "190:101", "191:100", "191:101"}),
        )

    def test_large_square_covers_full_interior(self):
        geom = Geometry(POLYGON, [[[10, 10], [12, 10], [12, 12],
                                   [10, 12], [10, 10]]])
        self.assertEqual(len(self.grid.coverage(geom)), 9)

    def test_polygon_with_hole_does_not_index_hole_center(self):
        ring = [[0, 0], [4, 0], [4, 4], [0, 4], [0, 0]]
        hole = [[1, 1], [3, 1], [3, 3], [1, 3], [1, 1]]
        geom = Geometry(POLYGON, [ring, hole])
        coverage = self.grid.coverage(geom)
        # 洞中心 (2,2) 所在格 182:092 只能通过边覆盖进入，检查内部点未被加入
        self.assertIn("180:090", coverage)
        self.assertNotIn("182:092", coverage)


class AntimeridianGridTest(unittest.TestCase):
    def setUp(self):
        self.grid = Grid(Decimal("1"))

    def test_line_wraps_around_dateline(self):
        geom = Geometry(LINESTRING, [[179.9, 0], [-179.9, 0]])
        self.assertEqual(
            self.grid.coverage(geom), frozenset({"359:090", "000:090"})
        )

    def test_line_wraps_in_both_directions(self):
        east_to_west = Geometry(LINESTRING, [[179.5, 0], [-179.5, 0]])
        west_to_east = Geometry(LINESTRING, [[-179.5, 0], [179.5, 0]])
        expected = frozenset({"359:090", "000:090"})
        self.assertEqual(self.grid.coverage(east_to_west), expected)
        self.assertEqual(self.grid.coverage(west_to_east), expected)

    def test_dateline_polygon_indexes_both_sides_but_one_home(self):
        geom = Geometry(POLYGON, [
            [[179, 0], [-179, 0], [-179, 2], [179, 2], [179, 0]]
        ])
        coverage = self.grid.coverage(geom)
        self.assertIn("359:090", coverage)
        self.assertIn("000:090", coverage)
        self.assertEqual(self.grid.anchor_key(geom), "359:090")

    def test_repeated_dateline_crossing_terminates_and_wraps(self):
        geom = Geometry(LINESTRING, [[170, 0], [-170, 0], [170, 0]])
        coverage = self.grid.coverage(geom)
        self.assertIn("350:090", coverage)
        self.assertIn("010:090", coverage)
        self.assertEqual(self.grid.anchor_key(geom), "350:090")


class DeterminismTest(unittest.TestCase):
    def test_same_coordinates_same_key_on_fine_grid(self):
        grid = Grid(Decimal("0.5"))
        key_a = grid.key_for_point(Decimal("10.2500000"), Decimal("20.7500000"))
        key_b = grid.key_for_point(Decimal("10.25"), Decimal("20.75"))
        self.assertEqual(key_a, key_b)
        self.assertEqual(key_a, "380:221")


if __name__ == "__main__":
    unittest.main()
