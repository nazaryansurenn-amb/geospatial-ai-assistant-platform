from datetime import date, timedelta
import unittest
import numpy as np
from analysis import summarize


class ScreeningTests(unittest.TestCase):
    def setUp(self):
        self.dates = ['2026-04-01', '2026-05-01', '2026-06-01', '2026-07-01', '2026-08-01']
        self.samples = [{'ndvi': np.array([.6, .6, np.nan, .1]),
                         'ndmi': np.array([.2, .2, np.nan, -.2]),
                         'clear': np.array([True, True, False, True])} for _ in self.dates]
        self.areas = np.array([100., 200., 300., 400.])
        self.lc = np.array([40, 10, 40, 40])
        days = [(date(2026, 3, 1)+timedelta(days=i)).isoformat() for i in range(160)]
        self.weather = {'daily': {'time': days, 'precipitation_sum': [0.]*len(days),
                                  'et0_fao_evapotranspiration': [5.]*len(days)}}

    def result(self, weather=True):
        return summarize(self.samples, self.dates, self.areas, self.lc,
                         self.weather if weather else None)[0]

    def test_natural_vegetation_is_not_called_cropland(self):
        r = self.result()
        self.assertAlmostEqual(r['repeated_vegetation_ha'], .03)
        self.assertAlmostEqual(r['active_in_historical_cropland_ha'], .01)

    def test_missing_is_not_zero_activity(self):
        r = self.result()
        self.assertAlmostEqual(r['insufficient_observations_ha'], .03)
        self.assertAlmostEqual(r['covered_ha'], .07)
        self.assertAlmostEqual(r['boundary_ha'], .1)

    def test_irrigation_is_proxy_not_confirmation(self):
        r = self.result()
        self.assertAlmostEqual(r['summer_irrigation_compatible_proxy_ha'], .01)
        self.assertIsNone(r['confirmed_irrigated_ha'])

    def test_missing_weather_does_not_produce_irrigation_estimate(self):
        self.assertIsNone(self.result(False)['summer_irrigation_compatible_proxy_ha'])

    def test_rain_removes_dry_context(self):
        self.weather['daily']['precipitation_sum'] = [10.]*len(self.weather['daily']['time'])
        self.assertEqual(self.result()['summer_irrigation_compatible_proxy_ha'], 0)

    def test_one_green_date_is_not_sustained_activity(self):
        for row in self.samples[1:]:
            row['ndvi'][0] = .1
        self.assertEqual(self.result()['active_in_historical_cropland_ha'], 0)


if __name__ == '__main__':
    unittest.main()
