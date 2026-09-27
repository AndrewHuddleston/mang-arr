"""Page-by-page fetching for sources whose image server refuses bursts:
the spacing between requests to one source (limits.Spacer). Nothing here
sleeps for real or reaches a network."""
import threading
import unittest

from mangarr import limits


class FakeClock:
    def __init__(self):
        self.t = 100.0
        self.lock = threading.Lock()

    def now(self):
        with self.lock:
            return self.t

    def advance(self, secs):
        with self.lock:
            self.t += secs


class SpacerTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.waits = []

        def pause(secs, should_cancel=None):
            self.waits.append(round(secs, 6))
            if should_cancel and should_cancel():
                return True
            self.clock.advance(max(secs, 0.0))
            return False
        self.spacer = limits.Spacer(clock=self.clock.now, pause=pause)

    def test_start_to_start_per_key(self):
        for _ in range(3):
            self.assertFalse(self.spacer.wait("weeb", 1.0))
        self.assertEqual(self.waits, [0.0, 1.0, 1.0])
        self.clock.advance(0.4)                             # 0.4 s of work after the last start
        self.spacer.wait("weeb", 1.0)
        self.assertEqual(self.waits[-1], 0.6)               # measured from the start, not the end
        self.clock.advance(5)
        self.spacer.wait("weeb", 1.0)
        self.assertEqual(self.waits[-1], 0.0)               # long idle: no wait

    def test_keys_do_not_wait_for_each_other(self):
        self.spacer.wait("weeb", 3.0)
        self.spacer.wait("bato", 3.0)
        self.spacer.wait("comick", 3.0)
        self.assertEqual(self.waits, [0.0, 0.0, 0.0])

    def test_callers_in_other_threads_queue_up(self):
        clock, gaps = FakeClock(), []
        spacer = limits.Spacer(clock=clock.now, pause=lambda secs, c=None: gaps.append(secs) or False)
        threads = [threading.Thread(target=spacer.wait, args=("weeb", 2.0)) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        self.assertEqual(sorted(gaps), [0.0, 2.0, 4.0, 6.0, 8.0])     # every one its own slot, none shared

    def test_cancel(self):
        self.spacer.wait("weeb", 10.0)
        self.assertTrue(self.spacer.wait("weeb", 10.0, should_cancel=lambda: True))

    def test_old_keys_are_pruned(self):
        self.spacer.wait("old", 1.0)
        self.clock.advance(7200)
        self.spacer.wait("new", 1.0)
        self.spacer.prune(3600)
        self.assertEqual(set(self.spacer._next), {"new"})


if __name__ == "__main__":
    unittest.main()
