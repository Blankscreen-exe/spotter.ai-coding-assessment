from django.test import SimpleTestCase

from planner.providers.polyline import decode_polyline


class DecodePolylineTests(SimpleTestCase):
    def test_reference_example(self):
        # The worked example from the format's specification.
        decoded = decode_polyline('_p~iF~ps|U_ulLnnqC_mqNvxq`@')
        self.assertEqual(decoded.tolist(), [[-120.2, 38.5], [-120.95, 40.7], [-126.453, 43.252]])

    def test_single_point(self):
        self.assertEqual(decode_polyline('_p~iF~ps|U').tolist(), [[-120.2, 38.5]])

    def test_empty(self):
        self.assertEqual(decode_polyline('').shape, (0, 2))
