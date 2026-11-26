import asyncio
import os
import unittest

from app import core


def meta(**kw):
    m = {"k": "abc", "i": 0, "n": 1, "s": 5, "f": "a.txt", "u": 1, "o": "x", "t": 1, "c": 1}
    m.update(kw)
    return m


class CoreTest(unittest.TestCase):
    def test_caption_roundtrip(self):
        m = meta(f="फ़ाइल 🎬 #TGF1 {.mkv", a=list(range(10 ** 9, 10 ** 9 + 20)))
        text = core.make_caption(m)
        self.assertEqual(core.parse_caption(text), m)
        self.assertLessEqual(len(text.encode("utf-16-le")) // 2, 1024)
        long = meta(f=core.clean_name("🎬" * 300 + ".mkv"), o="🎬" * 40,
                    p=core.clean_name("🎬" * 300 + ".001"), a=m["a"])
        self.assertLessEqual(len(core.make_caption(long).encode("utf-16-le")) // 2, 1024)
        self.assertIsNone(core.parse_caption("hello"))
        self.assertIsNone(core.parse_caption("#TGF1 {bad"))

    def test_index_and_access(self):
        ix = core.Index()
        ix.add(11, meta(i=1, n=2, s=3))
        self.assertIsNone(ix.get("abc"))
        ix.add(10, meta(n=2, a=[7]))
        rec = ix.get("abc")
        self.assertEqual((rec.size, [p[0] for p in rec.ordered]), (8, [10, 11]))
        self.assertIsNone(ix.add(99, meta(u=2)))
        rec.allowed = [7]
        self.assertTrue(rec.can_access(1) and rec.can_access(7))
        self.assertFalse(rec.can_access(8))
        ix.remove_message(10)
        self.assertIsNone(ix.get("abc"))

    def test_tokens(self):
        t = core.sign("s", {"up": 5}, 60)
        self.assertEqual(core.unsign("s", t)["up"], 5)
        self.assertIsNone(core.unsign("other", t))
        self.assertIsNone(core.unsign("s", "x" + t))
        self.assertIsNone(core.unsign("s", core.sign("s", {}, -1)))
        self.assertIsNone(core.unsign("s", "garbage"))

    def test_split_detection(self):
        self.assertEqual(core.detect_split(["m.mkv.002", "m.mkv.001"]), ("m.mkv", [1, 0]))
        self.assertEqual(core.detect_split(["a.part001", "a.part002"])[0], "a")
        self.assertIsNone(core.detect_split(["a.001", "a.003"]))
        self.assertIsNone(core.detect_split(["a.001", "b.002"]))
        self.assertIsNone(core.detect_split(["a.part1.rar", "a.part2.rar"]))

    def test_ranges(self):
        self.assertEqual(core.parse_range(None, 10), (0, 9, False))
        self.assertEqual(core.parse_range("bytes=2-", 10), (2, 9, True))
        self.assertEqual(core.parse_range("bytes=-3", 10), (7, 9, True))
        self.assertEqual(core.parse_range("bytes=0-99", 10), (0, 9, True))
        for bad in ("bytes=10-", "bytes=5-2", "bytes=-0"):
            with self.assertRaises(ValueError):
                core.parse_range(bad, 10)
        self.assertEqual(core.plan([4, 4, 2], 3, 8), [(0, 3, 1), (1, 0, 4), (2, 0, 1)])

    def test_real_limits(self):
        self.assertLess(core.VOLUME, 2000 * 1024 * 1024)
        self.assertEqual(core.VOLUME % core.PART, 0)
        up = core.Uploader(10 * 1024 ** 3, None, None)
        self.assertEqual(up.count, 6)


class UploaderTest(unittest.TestCase):
    def run_upload(self, size, chunk, fail_every=0, fail_finish=False):
        data = os.urandom(size)
        parts, volumes, calls = {}, [], [0, 0]

        async def save(fid, i, total, block, big):
            calls[0] += 1
            if fail_every and calls[0] % fail_every == 0:
                raise IOError("flaky")
            self.assertLessEqual(len(block), 8)
            parts[(fid, i)] = block

        async def finish(vol, fid, total, vsize, big):
            calls[1] += 1
            if fail_finish and calls[1] % 2:
                raise IOError("flaky")
            blob = b"".join(parts[(fid, i)] for i in range(total))
            self.assertEqual(len(blob), vsize)
            self.assertEqual(big, vsize > 16)
            volumes.append(blob)
            return 100 + vol

        async def go():
            up = core.Uploader(size, save, finish, volume=40, part=8, parallel=3, big=16)
            while not up.done:
                try:
                    if up.received < size:
                        await up.feed(data[up.received:up.received + chunk])
                    else:
                        await up.flush()
                except IOError:
                    pass
            return up

        up = asyncio.run(go())
        self.assertEqual(b"".join(volumes), data)
        self.assertEqual((up.sent, len(up.messages)), (size, (size + 39) // 40))

    def test_many_shapes(self):
        for size in (1, 7, 8, 39, 40, 41, 80, 123):
            for chunk in (1, 5, 8, 33, 200):
                self.run_upload(size, chunk)

    def test_recovers_from_failures(self):
        self.run_upload(500, 33, fail_every=5)
        self.run_upload(121, 50, fail_every=4, fail_finish=True)

    def test_rejects_extra_data(self):
        up = core.Uploader(3, None, None)
        with self.assertRaises(ValueError):
            asyncio.run(up.feed(b"abcd"))


if __name__ == "__main__":
    unittest.main()
