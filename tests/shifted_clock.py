"""Run the whole suite with the clock moved forward (default 400 days): a test that only passes on today's date fails
here long before that date arrives. CI runs it after the normal suite. Needs `pip install time-machine` (test-only).

    python tests/shifted_clock.py [days]
"""
import datetime as dt
import sys
import unittest

import time_machine

days = int(sys.argv[1]) if len(sys.argv) > 1 else 400
with time_machine.travel(dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days), tick=True):
    result = unittest.main(module=None, argv=["unittest", "discover", "-s", "tests"], exit=False).result
sys.exit(0 if result.wasSuccessful() else 1)
