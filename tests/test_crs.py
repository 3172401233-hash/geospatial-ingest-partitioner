import unittest
from decimal import Decimal

from geo_ingest.crs import (
    DEFAULT_PRECISION,
    EPSG_3857,
    EPSG_4326,
    normalize_coordinate,
)
from geo_ingest.errors import InvalidCoordinateError


class NormalizeCoordinateTest(unittest.TestCase):
    def test_quantizes_to_fixed_precision(self):
        lon, lat = normalize_coordinate("10.123456789", 20.555555555)
        self.assertEqual(lon, Decimal("10.1234568"))
        self.assertEqual(lat, Decimal("20.5555556"))

    def test_float_and_string_produce_identical_result(self):
        from_float = normalize_coordinate(10.1, 20.2)
        from_string = normalize_coordinate("10.1", "20.2")
        self.assertEqual(from_float, from_string)

    def test_half_even_rounding(self):
        lon, _ = normalize_coordinate("1.23456785", 0, precision=7)
        self.assertEqual(lon, Decimal("1.2345678"))
        lon, _ = normalize_coordinate("1.23456775", 0, precision=7)
        self.assertEqual(lon, Decimal("1.2345678"))

    def test_equivalent_precision_representations_collapse(self):
        a = normalize_coordinate(10, 20)
        b = normalize_coordinate("10.0000000000", "20.0000000000")
        self.assertEqual(a, b)

    def test_rejects_out_of_range_lon_lat(self):
        with self.assertRaises(InvalidCoordinateError):
            normalize_coordinate(180.0000001, 0)
        with self.assertRaises(InvalidCoordinateError):
            normalize_coordinate(0, 90.1)
        with self.assertRaises(InvalidCoordinateError):
            normalize_coordinate(-181, 0)

    def test_rejects_nan_and_infinity(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(InvalidCoordinateError):
                normalize_coordinate(bad, 0)
        with self.assertRaises(InvalidCoordinateError):
            normalize_coordinate("NaN", "0")

    def test_rejects_garbage_and_unsupported_srid(self):
        with self.assertRaises(InvalidCoordinateError):
            normalize_coordinate("east", 0)
        with self.assertRaises(InvalidCoordinateError):
            normalize_coordinate(0, 0, srid=4490)

    def test_web_mercator_roundtrip(self):
        lon, lat = normalize_coordinate(
            1113194.9079327357, 2273030.926987689, srid=EPSG_3857
        )
        self.assertEqual(lon, Decimal("10.0000000"))
        self.assertEqual(lat, Decimal("20.0000000"))

    def test_equator_and_origin_in_mercator(self):
        lon, lat = normalize_coordinate(0, 0, srid=EPSG_3857)
        self.assertEqual((lon, lat), (Decimal("0.0000000"), Decimal("0E-7")))

    def test_closed_bounds_are_valid(self):
        lon, lat = normalize_coordinate(180, 90)
        self.assertEqual((lon, lat), (Decimal("180.0000000"), Decimal("90.0000000")))
        lon, lat = normalize_coordinate(-180, -90)
        self.assertEqual((lon, lat), (Decimal("-180.0000000"), Decimal("-90.0000000")))

    def test_custom_precision(self):
        lon, lat = normalize_coordinate("1.234567", "2.345678", precision=3)
        self.assertEqual((lon, lat), (Decimal("1.235"), Decimal("2.346")))
        self.assertEqual(DEFAULT_PRECISION, 7)


if __name__ == "__main__":
    unittest.main()
