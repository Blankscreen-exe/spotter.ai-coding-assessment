from unittest import mock

import requests
from django.db import DatabaseError
from django.test import TestCase

from planner import warmup
from planner.services import stations


class WarmUpTests(TestCase):
    def setUp(self):
        stations.reset_index()
        self.addCleanup(stations.reset_index)
        # warm_up closes its database connections; inside a test that would end the test's transaction.
        patcher = mock.patch.object(warmup.connections, 'close_all')
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_loads_the_station_index_and_opens_the_provider_connection(self):
        with mock.patch.object(requests.Session, 'head') as head:
            with self.assertLogs('planner.warmup', level='INFO') as logs:
                warmup.warm_up()
        self.assertEqual(stations.get_index.cache_info().currsize, 1)  # the stations are in memory
        self.assertIn('OSRM connection open', logs.output[0])
        head.assert_called_once()
        self.assertEqual(head.call_args.args, ('https://router.project-osrm.org',))

    def test_unreachable_provider_does_not_stop_the_server(self):
        with mock.patch.object(requests.Session, 'head', side_effect=requests.ConnectionError):
            with self.assertLogs('planner.warmup', level='INFO') as logs:
                warmup.warm_up()
        self.assertEqual(stations.get_index.cache_info().currsize, 1)  # the stations are in memory
        self.assertIn('OSRM connection not available', logs.output[0])

    def test_unmigrated_database_does_not_stop_the_server(self):
        with mock.patch.object(warmup, 'get_index', side_effect=DatabaseError('no such table')):
            with mock.patch.object(requests.Session, 'head') as head:
                with self.assertLogs('planner.warmup', level='WARNING'):
                    warmup.warm_up()
        head.assert_not_called()
