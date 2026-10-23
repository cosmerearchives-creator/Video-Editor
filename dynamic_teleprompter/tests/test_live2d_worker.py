import threading
import time
import unittest

from live2d_worker import LatestPoseWorker


class WorkerTests(unittest.TestCase):
    def worker(self, processor, **kwargs):
        worker = LatestPoseWorker(processor, interval=0, **kwargs)
        self.addCleanup(worker.close, 2)
        return worker

    def wait_until(self, predicate):
        deadline = time.monotonic() + 2
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail('Worker did not complete')
            time.sleep(.001)

    def test_slow_render_keeps_requests_nonblocking_and_coalesces_poses(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        seen, threads = [], []

        def render(key, pose):
            seen.append(pose)
            threads.append(threading.get_ident())
            if pose == 0:
                entered.set()
                release.wait(2)
            return pose

        worker = self.worker(render)
        worker.request('model', 0, 0)
        self.assertTrue(entered.wait(1))
        started = time.monotonic()
        for pose in range(1, 101):
            self.assertIsNone(worker.request('model', pose, pose)[0])
        self.assertLess(time.monotonic() - started, .1)
        release.set()
        self.wait_until(lambda: worker.stats('model').get('completed') == 2)
        self.assertEqual(seen, [0, 100])
        self.assertEqual(worker.request('model', 100, 100), (100, ''))
        self.assertEqual(len(set(threads)), 1)
        self.assertNotEqual(threads[0], threading.get_ident())

    def test_failure_preserves_last_image_and_does_not_spin(self):
        def render(key, pose):
            if pose == 2:
                raise RuntimeError('bad model')
            return pose
        worker = self.worker(render)
        worker.request('model', 1, 1)
        self.wait_until(lambda: worker.stats('model').get('completed') == 1)
        worker.request('model', 2, 2)
        self.wait_until(lambda: worker.stats('model').get('completed') == 2)
        self.assertEqual(worker.request('model', 2, 2), (1, 'bad model'))
        worker.request('model', 3, 3)
        self.wait_until(lambda: worker.stats('model').get('completed') == 3)
        self.assertEqual(worker.request('model', 3, 3), (3, ''))

    def test_close_does_not_wait_for_render_and_drops_pending_work(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def render(key, pose):
            entered.set()
            release.wait(2)
            finished.set()
            return pose
        worker = self.worker(render)
        worker.request('model', 1, 1)
        self.assertTrue(entered.wait(1))
        worker.request('model', 2, 2)
        worker.close()
        self.assertEqual(worker.request('model', 3, 3), (None, ''))
        release.set()
        self.assertTrue(finished.wait(1))

    def test_evicted_completion_does_not_replace_new_slot(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def render(key, pose):
            if pose == 1:
                entered.set()
                release.wait(2)
            return pose
        worker = self.worker(render, capacity=1)
        worker.request('model', 1, 1)
        self.assertTrue(entered.wait(1))
        worker.request('other', 2, 2)
        worker.request('model', 3, 3)
        release.set()
        self.wait_until(lambda: worker.stats('model').get('completed') == 1)
        self.assertEqual(worker.request('model', 3, 3), (3, ''))
        self.assertEqual(worker.stats('other'), {})


if __name__ == '__main__':
    unittest.main()
