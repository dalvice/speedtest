#!/usr/bin/env python3
"""Автотесты speedtest.py. Запуск:  python3 -m unittest -v test_speedtest

Поднимают локальный testserver.py на свободном порту, в интернет не ходят.
"""

import contextlib
import io
import os
import re
import subprocess
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import speedtest  # noqa: E402
import testserver  # noqa: E402

SCRIPT = os.path.join(HERE, "speedtest.py")
PAYLOAD_SIZE = len(testserver.PAYLOAD)


class ServerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # В окружении может быть настроен HTTP-прокси — для localhost он не нужен.
        cls._env = {k: os.environ.get(k) for k in ("no_proxy", "NO_PROXY")}
        os.environ["no_proxy"] = os.environ["NO_PROXY"] = "*"
        cls.server = testserver.start(0)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        for key, value in cls._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def setUp(self):
        testserver.reset_stats()

    def cli(self, *args, timeout=60):
        """Запускает скрипт как процесс. Возвращает (код, stdout, stderr)."""
        proc = subprocess.run(
            [sys.executable, SCRIPT, *args],
            capture_output=True, text=True, encoding="utf-8", timeout=timeout,
        )
        return proc.returncode, proc.stdout, proc.stderr


class TestRequirements(ServerCase):
    """Пункты технического задания."""

    def test_default_is_exactly_ten_requests(self):
        code, out, _ = self.cli(f"{self.base}/ok")
        self.assertEqual(code, 0)
        self.assertEqual(len(testserver.STATS["paths"]), 10)
        self.assertIn("успешных запросов:    10 из 10", out)

    def test_requests_are_sequential(self):
        # Сервер отвечает за 0.2 с. Последовательно 5 запросов займут >= 1 с,
        # параллельно уложились бы примерно в 0.2 с.
        started = time.monotonic()
        code, out, _ = self.cli(f"{self.base}/delay", "-n", "5")
        elapsed = time.monotonic() - started
        self.assertEqual(code, 0, out)
        self.assertGreaterEqual(elapsed, 5 * testserver.DELAY)
        # и среднее время запроса не меньше задержки сервера
        avg = float(re.search(r"среднее время ответа: ([\d.]+) с", out).group(1))
        self.assertGreaterEqual(avg, testserver.DELAY)

    def test_total_volume_and_average_time_and_speed_are_printed(self):
        code, out, _ = self.cli(f"{self.base}/ok")
        self.assertEqual(code, 0)
        self.assertIn(f"({10 * PAYLOAD_SIZE} байт)", out)
        self.assertRegex(out, r"среднее время ответа: \d+\.\d{3} с")
        self.assertRegex(out, r"СКОРОСТЬ СКАЧИВАНИЯ:\s+\d+\.\d{2} Mbit/s \(\d+\.\d{2} MiB/s\)")

    def test_every_response_is_read_to_the_end(self):
        code, out, _ = self.cli(f"{self.base}/ok", "-n", "3")
        self.assertEqual(code, 0)
        size = f"{PAYLOAD_SIZE / 1024:.2f} KiB"
        # размер полного ответа: по одному разу в каждой из 3 строк + в «среднем размере»
        self.assertEqual(out.count(size), 4, out)

    def test_custom_request_count(self):
        code, _, _ = self.cli(f"{self.base}/ok", "-n", "4")
        self.assertEqual(code, 0)
        self.assertEqual(len(testserver.STATS["paths"]), 4)


class TestCalculations(unittest.TestCase):
    def render(self, attempts, interrupted=False):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            code = speedtest.report(attempts, interrupted)
        return code, buf.getvalue()

    def test_numbers(self):
        attempts = [speedtest.Attempt(i, 0.5, 1_000_000) for i in range(4)]
        code, out = self.render(attempts)
        self.assertEqual(code, 0)
        self.assertIn("среднее время ответа: 0.500 с", out)
        self.assertIn("общее время загрузки: 2.000 с", out)
        self.assertIn("(4000000 байт)", out)
        # 4 000 000 байт * 8 / 2 с = 16 Mbit/s = 1.91 MiB/s
        self.assertIn("16.00 Mbit/s (1.91 MiB/s)", out)

    def test_failed_attempts_are_excluded_but_reported(self):
        attempts = [
            speedtest.Attempt(1, 1.0, 2_000_000),
            speedtest.Attempt(2, 9.0, 5, error="boom"),
        ]
        code, out = self.render(attempts)
        self.assertEqual(code, 1)
        self.assertIn("1 из 2 (ошибок: 1)", out)
        self.assertIn("(2000000 байт)", out)
        self.assertIn("16.00 Mbit/s", out)
        # среднее — только по успешным: 1.0 с, а не (1.0 + 9.0) / 2
        self.assertIn("среднее время ответа: 1.000 с", out)
        self.assertIn("общее время загрузки: 1.000 с", out)

    def test_all_failed(self):
        code, _ = self.render([speedtest.Attempt(1, 0.1, 0, error="x")])
        self.assertEqual(code, 1)

    def test_zero_total_time_does_not_crash(self):
        code, out = self.render([speedtest.Attempt(1, 0.0, 10)])
        self.assertEqual(code, 0)
        self.assertIn("0.00 Mbit/s", out)


class TestBadResponses(ServerCase):
    def assertFails(self, path, message, *extra):
        code, out, err = self.cli(f"{self.base}{path}", "-n", "2", *extra)
        self.assertEqual(code, 1, out)
        self.assertIn(message, out)
        self.assertNotIn("Traceback", out + err)
        # весь отчёт, включая итоговую строку о провале, должен быть в stdout
        self.assertIn("Ни один запрос не выполнен успешно", out)
        self.assertEqual(err.strip(), "")

    def test_truncated_by_content_length(self):
        self.assertFails("/truncated", "неполный ответ: получено 1024 из 1048576 байт")

    def test_truncated_chunked(self):
        self.assertFails("/chunked-truncated", "соединение оборвано, получено 1024 байт")

    def test_empty_body(self):
        self.assertFails("/empty", "пустое тело")

    def test_http_errors(self):
        self.assertFails("/missing", "HTTP 404")
        self.assertFails("/error500", "HTTP 500")

    def test_timeout_on_stalled_response(self):
        self.assertFails("/slow", "таймаут 1 с", "-t", "1")

    def test_timeout_is_total_not_per_read(self):
        started = time.monotonic()
        self.assertFails("/trickle", "таймаут 2 с", "-t", "2")
        # Два запроса по ~2 с. Если бы лимит действовал на каждое чтение, висели бы минуты.
        self.assertLess(time.monotonic() - started, 8)

    def test_connection_refused(self):
        code, out, _ = self.cli("http://127.0.0.1:1/x", "-n", "1")
        self.assertEqual(code, 1)
        self.assertIn("ОШИБКА", out)


class TestGoodVariants(ServerCase):
    def test_chunked_ignores_bogus_content_length(self):
        code, out, _ = self.cli(f"{self.base}/chunked-with-cl", "-n", "2")
        self.assertEqual(code, 0, out)
        self.assertIn(f"({2 * PAYLOAD_SIZE} байт)", out)

    def test_chunked(self):
        code, _, _ = self.cli(f"{self.base}/chunked", "-n", "2")
        self.assertEqual(code, 0)

    def test_redirect_is_followed(self):
        code, out, _ = self.cli(f"{self.base}/redirect", "-n", "2")
        self.assertEqual(code, 0, out)
        self.assertIn(f"({2 * PAYLOAD_SIZE} байт)", out)

    def test_non_ascii_path(self):
        code, out, _ = self.cli(f"{self.base}/файл", "-n", "2")
        self.assertEqual(code, 0, out)

    def test_gzip_is_flagged(self):
        code, out, _ = self.cli(f"{self.base}/gzip", "-n", "2")
        self.assertEqual(code, 0)
        self.assertIn("Предупреждения", out)
        self.assertIn("gzip", out)

    def test_response_without_length_is_flagged_but_counted(self):
        code, out, _ = self.cli(f"{self.base}/no-length", "-n", "2")
        self.assertEqual(code, 0, out)
        self.assertIn(f"({2 * PAYLOAD_SIZE} байт)", out)
        self.assertIn("не сообщил размер ответа", out)

    def test_verifiable_responses_are_not_flagged(self):
        code, out, _ = self.cli(f"{self.base}/ok", "-n", "2")
        self.assertEqual(code, 0)
        self.assertNotIn("не сообщил размер ответа", out)

    def test_redirect_loop_error_stays_on_one_line(self):
        code, out, _ = self.cli(f"{self.base}/redirect-loop", "-n", "1")
        self.assertEqual(code, 1)
        errors = [ln for ln in out.splitlines() if "ОШИБКА" in ln]
        self.assertEqual(len(errors), 1, out)
        self.assertIn("HTTP 302", errors[0])
        self.assertLessEqual(len(errors[0]), 200)
        # Сообщение urllib про петлю многострочное. Оно должно целиком уместиться
        # в одной строке отчёта, а не растечься по следующим.
        self.assertIn("infinite loop", errors[0])
        self.assertIn("Found", errors[0])
        body = out[out.index(errors[0]) + len(errors[0]):]
        self.assertNotIn("30x", body, "хвост сообщения протёк в следующие строки")

    def test_normalized_url_is_shown_when_it_differs(self):
        _, out, _ = self.cli(f"{self.base}/файл#frag", "-n", "1")
        self.assertIn("Запрашиваю: ", out)
        self.assertIn("%D1%84", out)

    def test_plain_url_is_not_rewritten_in_output(self):
        _, out, _ = self.cli(f"{self.base}/ok", "-n", "1")
        self.assertNotIn("Запрашиваю:", out)

    def test_broken_pipe_is_silent(self):
        # `speedtest.py ... | head -3`: получатель закрывает поток на середине.
        proc = subprocess.run(
            f'"{sys.executable}" "{SCRIPT}" {self.base}/ok -n 10 | head -3',
            shell=True, capture_output=True, text=True, encoding="utf-8", timeout=60,
        )
        self.assertNotIn("Traceback", proc.stderr)
        self.assertNotIn("Exception ignored", proc.stderr)
        self.assertEqual(proc.stderr.strip(), "")

    def test_single_request_is_flagged(self):
        code, out, _ = self.cli(f"{self.base}/ok", "-n", "1")
        self.assertEqual(code, 0)
        self.assertIn("всего один", out)

    def test_cache_bust_makes_urls_unique(self):
        self.cli(f"{self.base}/ok?a=1", "-n", "3", "--cache-bust")
        paths = testserver.STATS["paths"]
        self.assertEqual(len(set(paths)), 3, paths)
        self.assertTrue(all(p.startswith("/ok?a=1&_cb=") for p in paths), paths)

    def test_without_cache_bust_url_is_untouched(self):
        self.cli(f"{self.base}/ok?a=1", "-n", "2")
        self.assertEqual(set(testserver.STATS["paths"]), {"/ok?a=1"})

    def test_mixed_run_reports_partial_failure(self):
        # Первый и третий запрос — 404, второй и четвёртый — ok.
        urls = [f"{self.base}/missing", f"{self.base}/ok"]
        real = speedtest.download_once
        speedtest.download_once = lambda url, i, t, c: real(urls[i % 2], i, t, c)
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = speedtest.main([f"{self.base}/ok", "-n", "4"])
        finally:
            speedtest.download_once = real
        self.assertEqual(code, 1)
        self.assertIn("2 из 4 (ошибок: 2)", buf.getvalue())


class TestUrlNormalization(unittest.TestCase):
    def test_valid(self):
        n = speedtest.normalize_url
        self.assertEqual(n("http://example.com"), "http://example.com/")
        self.assertEqual(n("  https://example.com/a b.jpg  "), "https://example.com/a%20b.jpg")
        self.assertEqual(n("http://example.com/файл.jpg"),
                         "http://example.com/%D1%84%D0%B0%D0%B9%D0%BB.jpg")
        self.assertEqual(n("http://example.com/a%20b?q=1"), "http://example.com/a%20b?q=1")
        self.assertEqual(n("http://example.com/x?q=значение"),
                         "http://example.com/x?q=%D0%B7%D0%BD%D0%B0%D1%87%D0%B5%D0%BD%D0%B8%D0%B5")
        self.assertEqual(n("http://example.com/x#frag"), "http://example.com/x")
        self.assertEqual(n("http://пример.рф/x"), "http://xn--e1afmkfd.xn--p1ai/x")
        self.assertEqual(n("http://127.0.0.1:8080/x"), "http://127.0.0.1:8080/x")
        self.assertEqual(n("http://[::1]:8080/x"), "http://[::1]:8080/x")

    def test_invalid(self):
        for bad in ("ftp://x.com", "file:///etc/passwd", "example.com/x", "https://",
                    "http://127.0.0.1:99999/", "http://127.0.0.1:abc/", "http://u:p@x.com/",
                    "http://" + "a" * 70 + ".com/", ""):
            with self.subTest(url=bad):
                with self.assertRaises(speedtest.UrlError):
                    speedtest.normalize_url(bad)


class TestCommandLine(unittest.TestCase):
    def run_cli(self, *args):
        proc = subprocess.run([sys.executable, SCRIPT, *args],
                              capture_output=True, text=True, encoding="utf-8", timeout=30)
        return proc.returncode, proc.stdout + proc.stderr

    def test_no_arguments(self):
        code, out = self.run_cli()
        self.assertEqual(code, 2)
        self.assertIn("url", out)

    def test_bad_arguments(self):
        for args in (["http://x.com", "-n", "0"], ["http://x.com", "-n", "abc"],
                     ["http://x.com", "-t", "nan"], ["http://x.com", "-t", "inf"],
                     ["http://x.com", "-t", "-1"], ["http://x.com", "-c", "0"],
                     ["ftp://x.com"], ["https://"]):
            with self.subTest(args=args):
                code, out = self.run_cli(*args)
                self.assertEqual(code, 2, out)
                self.assertNotIn("Traceback", out)

    def test_help(self):
        code, out = self.run_cli("--help")
        self.assertEqual(code, 0)
        self.assertIn("--cache-bust", out)

    def test_interrupt_prints_partial_report(self):
        calls = {"n": 0}
        real = speedtest.download_once

        def fake(url, index, timeout, chunk_size):
            calls["n"] += 1
            if calls["n"] == 3:
                raise KeyboardInterrupt
            return speedtest.Attempt(index, 0.5, 1_000_000)

        speedtest.download_once = fake
        try:
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = speedtest.main(["https://example.com/x"])
        finally:
            speedtest.download_once = real
        self.assertEqual(code, 130)
        self.assertIn("2 из 2", out.getvalue())
        self.assertIn("прогон прерван", out.getvalue())
        self.assertIn("Прервано пользователем", err.getvalue())

    def test_output_survives_non_utf8_console(self):
        env = dict(os.environ, PYTHONIOENCODING="ascii", PYTHONUTF8="0")
        proc = subprocess.run([sys.executable, SCRIPT, "http://127.0.0.1:1/x", "-n", "1"],
                              capture_output=True, env=env, timeout=30)
        self.assertEqual(proc.returncode, 1)
        self.assertNotIn(b"Traceback", proc.stderr)
        self.assertTrue(re.search(rb"[?]{3,}", proc.stdout), proc.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
