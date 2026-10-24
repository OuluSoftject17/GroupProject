import contextlib
import io
import os
import tempfile
import unittest

import benchmark_webedge


def run_benchmark(*args):
    output = io.StringIO()

    # Reports go to a temporary directory so tests do not fill
    # benchmark-results/.
    with tempfile.TemporaryDirectory() as directory:
        with contextlib.redirect_stdout(output):
            status = benchmark_webedge.main([
                "--iterations", "3",
                "--warmup", "1",
                "--keygen-iterations", "2",
                "--output-dir", directory,
                *args,
            ])

        reports = os.listdir(directory)

    return status, output.getvalue(), reports


class BenchmarkTests(unittest.TestCase):

    def test_summarize(self):
        stats = benchmark_webedge.summarize(
            [ms * 1_000_000 for ms in range(1, 101)]
        )

        self.assertEqual(stats["median"], 50.5)
        self.assertAlmostEqual(stats["p95"], 95.05)
        self.assertEqual(stats["min"], 1)

    def test_report_covers_both_methods(self):
        status, output, _ = run_benchmark()

        self.assertEqual(status, 0)

        for method in ("RSA-2048 OAEP", "ML-KEM-768"):
            self.assertIn(f"| Whole request (all four) | {method} |", output)

        # 1,088-byte ML-KEM-768 ciphertext, Base64 encoded.
        self.assertIn("| Key header | 392 | 1452 |", output)

    def test_http_requests_succeed(self):
        status, output, _ = run_benchmark(
            "--http",
            "--http-iterations",
            "2",
        )

        self.assertEqual(status, 0)
        self.assertIn("| Whole HTTP request | ML-KEM-768 |", output)

    def test_report_is_saved(self):
        status, output, reports = run_benchmark()

        self.assertEqual(status, 0)
        self.assertEqual(len(reports), 1)
        self.assertRegex(
            reports[0],
            r"^webedge-benchmark-\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\.md$",
        )
        self.assertIn("Report saved to ", output)

    def test_existing_report_is_not_overwritten(self):
        started = benchmark_webedge.datetime.datetime(2026, 10, 20, 12, 0, 0)

        with tempfile.TemporaryDirectory() as directory:
            benchmark_webedge.save_report("first", directory, started)

            with self.assertRaises(FileExistsError):
                benchmark_webedge.save_report("second", directory, started)

    def test_too_few_iterations_is_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                benchmark_webedge.main(["--iterations", "1"])


if __name__ == "__main__":
    unittest.main()
