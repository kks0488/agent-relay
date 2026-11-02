import concurrent.futures
from contextlib import contextmanager
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import agent_relay as relay

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "agent_relay.py"
FAKE = Path(__file__).with_name("fake_engine.py")


@contextmanager
def api_server(response=None, status=200, delay=0, drip=False):
    calls = []
    response = response if response is not None else {
        "choices": [{"message": {"content": "A useful API answer."}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1000, "completion_tokens": 500},
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            calls.append({"path": self.path, "body": json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
                          "authorization": self.headers.get("Authorization")})
            time.sleep(delay)
            data = json.dumps(response).encode()
            try:
                self.send_response(status)
                if status == 302:
                    self.send_header("Location", "/redirected")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if drip:
                    for byte in data:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(0.04)
                else:
                    self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.brief = self.directory / "brief.md"
        self.brief.write_text("Check source.txt and explain the result.", encoding="utf-8")
        (self.directory / "source.txt").write_text("7\n", encoding="utf-8")
        self.output = self.directory / "out.md"
        self.trace = self.directory / "trace.jsonl"
        self.capture = self.directory / "capture.jsonl"
        self.config = self.directory / "relay.toml"
        self.environment = dict(os.environ, FAKE_API_KEY="test-" + "X" * 32, PYTHONDONTWRITEBYTECODE="1")

    def engine(self, behavior="success", name="fake", **extra):
        result = {"command": [sys.executable, str(FAKE), behavior, "--name", name,
                               "--trace", str(self.trace), "--capture", str(self.capture)]}
        result.update(extra)
        return result

    def api(self, url, **extra):
        result = {"type": "api", "base_url": url, "model": "example-model", "api_key_env": "FAKE_API_KEY",
                  "daily_budget_usd": 1, "input_usd_per_million": 1, "output_usd_per_million": 2,
                  "max_tokens": 10, "timeout": 3}
        result.update(extra)
        return result

    def configure(self, engines, **settings):
        settings = dict({"state_dir": "state", "timeout": 2, "wait_timeout": 2, "max_concurrent": 4}, **settings)
        lines = ["[relay]"]
        lines += [f"{key} = {json.dumps(value)}" for key, value in settings.items()]
        lines += ["", "[modes]"]
        lines += [f"{mode} = {json.dumps(list(engines))}" for mode in relay.MODES if mode != "cross" or len(engines) >= 2]
        for name, engine in engines.items():
            lines += ["", f"[engines.{name}]"]
            lines += [f"{key} = {json.dumps(value)}" for key, value in engine.items()]
        self.config.write_text("\n".join(lines), encoding="utf-8")

    def command(self, mode="research", output=None):
        return [sys.executable, str(SCRIPT), mode, str(self.brief), "-o", str(output or self.output),
                "-C", str(self.directory), "--config", str(self.config)]

    def run_relay(self, mode="research", output=None, expected=0):
        result = subprocess.run(self.command(mode, output), env=self.environment, capture_output=True,
                                text=True, timeout=12)
        self.assertEqual(result.returncode, expected, result.stderr)
        return result

    def events(self):
        return [json.loads(line) for line in self.trace.read_text().splitlines()]

    def log(self):
        return Path(str(self.output) + ".log").read_text()

    def ledger(self):
        return json.loads((self.directory / "state" / "costs.json").read_text())

    def test_fallback_order(self):
        engines = {name: self.engine(behavior, name) for name, behavior in [
            ("bad", "failure"), ("blank", "empty"), ("limited", "rate"),
            ("stderr_limited", "stderr-rate"), ("good", "success"), ("unused", "success")]}
        self.configure(engines)
        self.run_relay()
        self.assertEqual([x["name"] for x in self.events() if x["event"] == "start"], list(engines)[:-1])
        self.assertIn("useful answer", self.output.read_text())
        for reason in ("exit code 7", "empty result", "terminal engine error", "elapsed="):
            self.assertIn(reason, self.log())

    def test_timeout_falls_back(self):
        self.configure({"slow": self.engine("slow", timeout=0.1), "good": self.engine()})
        self.run_relay()
        self.assertIn("engine timeout", self.log())

    def test_missing_executable_falls_back(self):
        self.configure({"missing": {"command": ["relay-nonexistent-example-command"]}, "good": self.engine()})
        self.run_relay()
        self.assertIn("execution failure", self.log())

    def test_body_words_and_fenced_diagnostics_are_valid(self):
        for behavior in ("body", "quoted", "echo"):
            with self.subTest(behavior=behavior):
                self.configure({"good": self.engine(behavior), "unused": self.engine("failure")})
                self.run_relay()
                self.assertNotIn("fallback:", self.log())

    def test_only_terminal_diagnostics_trigger_fallback(self):
        self.assertIsNone(relay.result_failure("ERROR: recovered\nThe result is verified."))
        self.assertIsNone(relay.result_failure("The rate limit is a documented setting."))
        self.assertIsNone(relay.result_failure("The error is fixed."))
        for line in ("ERROR: disconnected", "error: failed", "HTTP 429 Too Many Requests", "429 quota",
                     "rate limit exceeded", "quota exhausted", "2026-01-01T12:00:00Z ERROR: quota"):
            with self.subTest(line=line):
                self.assertIn("terminal engine error", relay.result_failure("Useful text\n" + line))

    def test_review_prefix_both_languages(self):
        for language, expected in (("en", "No nitpicks"), ("ko", "꼬투리")):
            self.configure({"good": self.engine()}, review_language=language)
            self.run_relay("review")
            prompt = json.loads(self.capture.read_text().splitlines()[-1])["prompt"]
            self.assertTrue(prompt.startswith("# "))
            self.assertLess(prompt.index(expected), prompt.index(self.brief.read_text()))

    def test_cross_three_stages(self):
        self.configure({"alpha": self.engine("cross", "alpha"), "beta": self.engine("cross", "beta")})
        self.run_relay("cross")
        rows = [json.loads(line) for line in self.capture.read_text().splitlines()]
        self.assertEqual([x["name"] for x in rows], ["alpha", "beta", "alpha"])
        self.assertEqual(rows[0]["prompt"], rows[1]["prompt"])
        self.assertNotIn("## Answer A", rows[1]["prompt"])
        self.assertIn("## Answer A (alpha)", rows[2]["prompt"])
        self.assertIn("## Answer B (beta)", rows[2]["prompt"])
        self.assertIn("ONLY disagreements", rows[2]["prompt"])
        self.assertIn("value is 7", Path(str(self.output) + ".a.md").read_text())
        self.assertIn("value is 9", Path(str(self.output) + ".b.md").read_text())
        self.assertIn("source.txt:1", self.output.read_text())
        self.assertIn("Comparison table", self.output.read_text())

    def test_cross_fallback_keeps_engines_distinct(self):
        self.configure({"bad": self.engine("failure", "bad"), "alpha": self.engine("cross", "alpha"),
                        "beta": self.engine("cross", "beta")})
        self.run_relay("cross")
        self.assertIn("## Answer A (alpha)", json.loads(self.capture.read_text().splitlines()[-1])["prompt"])
        self.assertIn("## Answer B (beta)", json.loads(self.capture.read_text().splitlines()[-1])["prompt"])

    def test_cross_comparison_does_not_switch_engine(self):
        self.configure({"alpha": self.engine("cross-fail-comparison", "alpha"),
                        "beta": self.engine("cross", "beta")})
        self.run_relay("cross", expected=1)
        rows = [json.loads(line) for line in self.capture.read_text().splitlines()]
        self.assertEqual([x["name"] for x in rows], ["alpha", "beta", "alpha"])
        self.assertFalse(self.output.exists())
        self.assertTrue(Path(str(self.output) + ".a.md").exists())
        self.assertTrue(Path(str(self.output) + ".b.md").exists())

    def test_failed_run_preserves_previous_output(self):
        self.output.write_text("Previous valid result.")
        self.configure({"bad": self.engine("failure")})
        self.run_relay(expected=1)
        self.assertEqual(self.output.read_text(), "Previous valid result.")

    def test_output_file_template(self):
        engine = self.engine()
        engine["command"] += ["--output", "{output}"]
        engine["output"] = "file"
        self.configure({"good": engine})
        self.run_relay()
        self.assertIn("useful answer", self.output.read_text())

    def test_log_redaction(self):
        self.configure({"good": self.engine("secrets")})
        self.run_relay()
        for secret in ("sk-" + "S" * 30, "ghp_" + "T" * 36, "b7" * 24,
                       "opaque-example-value", "opaque token value"):
            self.assertNotIn(secret, self.log())
        self.assertGreaterEqual(self.log().count("[REDACTED]"), 5)

    def check_success_log_omits_response(self, output_mode):
        engine = self.engine("private-output", output=output_mode)
        if output_mode == "file":
            engine["command"] += ["--output", "{output}"]
        self.configure({"good": engine})
        self.run_relay()
        answer = "UNIQUE_FINAL_RESPONSE_응답"
        self.assertEqual(self.output.read_text(), answer + "\n")
        log = self.log()
        self.assertNotIn(answer, log)
        self.assertNotIn("UNIQUE_STDOUT_RESPONSE", log)
        self.assertIn("diagnostic preserved; token=[REDACTED]", log)
        self.assertNotIn("private-diagnostic-secret", log)
        self.assertIn("engine=good", log)
        self.assertRegex(log, r"elapsed=\d+\.\d+s")
        expected_bytes = len((answer + ("\n" if output_mode == "stdout" else "")).encode("utf-8"))
        self.assertIn(f"bytes={expected_bytes} success", log)

    def test_successful_file_output_body_is_not_logged(self):
        self.check_success_log_omits_response("file")

    def test_successful_stdout_body_is_not_logged(self):
        self.check_success_log_omits_response("stdout")

    def test_failed_file_output_keeps_redacted_diagnostics(self):
        engine = self.engine("private-output-failure", output="file")
        engine["command"] += ["--output", "{output}"]
        self.configure({"bad": engine})
        self.run_relay(expected=1)
        log = self.log()
        self.assertIn("UNIQUE_STDOUT_RESPONSE", log)
        self.assertIn("exit code 7", log)
        self.assertIn("diagnostic preserved; token=[REDACTED]", log)
        self.assertNotIn("private-diagnostic-secret", log)

    def test_redaction_happens_before_tail_truncation(self):
        self.configure({"good": self.engine("long-secret")})
        self.run_relay()
        self.assertNotIn("S" * 50, self.log())
        self.assertIn("[REDACTED]", self.log())

    def test_prompt_placeholder_is_literal_data(self):
        self.brief.write_text('Keep {output} and {workdir} literally; $(example) "quoted".')
        engine = self.engine()
        engine["command"] += ["--argument", "{prompt}"]
        self.configure({"good": engine})
        self.run_relay()
        row = json.loads(self.capture.read_text().splitlines()[0])
        self.assertEqual(row["argument"], self.brief.read_text())
        self.assertEqual(row["prompt"], self.brief.read_text())

    def test_unknown_environment_token_is_redacted(self):
        self.environment["EXAMPLE_SECRET"] = "opaque-example-value"
        self.configure({"good": self.engine("secrets")})
        self.run_relay()
        self.assertNotIn("opaque-example-value", self.log())

    def test_concurrent_limit(self):
        self.configure({"slow": self.engine("slow")}, max_concurrent=2, wait_timeout=5)
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            futures = [pool.submit(self.run_relay, output=self.directory / f"out{i}.md") for i in range(5)]
            for future in futures:
                future.result()
        active = peak = 0
        for event in sorted(self.events(), key=lambda x: x["time"]):
            active += 1 if event["event"] == "start" else -1
            peak = max(peak, active)
        self.assertEqual(peak, 2)
        self.assertEqual(active, 0)

    def test_wait_timeout_is_clear_and_bounded(self):
        self.configure({"good": self.engine()}, max_concurrent=1, wait_timeout=0.12)
        started = time.monotonic()
        with relay.file_lock(self.directory / "state" / "slots" / "0.lock", 1):
            result = self.run_relay(expected=1)
        self.assertIn("Execution slot wait timeout", result.stderr)
        self.assertLess(time.monotonic() - started, 2)
        self.assertFalse(self.trace.exists())
        self.run_relay()

    def test_locks_release_after_holder_dies(self):
        self.configure({"slow": self.engine("slow")}, max_concurrent=1)
        holder = subprocess.Popen(self.command(), env=self.environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 3
            while not self.trace.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(self.trace.exists())
            holder.kill()
            holder.wait(timeout=3)
            self.run_relay(output=self.directory / "next.md")
        finally:
            if holder.poll() is None:
                holder.kill()
                holder.wait()

    def test_api_usage_cost_and_cap(self):
        with api_server() as (url, calls):
            self.configure({"api": self.api(url, daily_budget_usd=0.002), "good": self.engine()})
            self.run_relay()
            self.assertEqual(self.output.read_text().strip(), "A useful API answer.")
            self.run_relay()
            self.assertEqual(len(calls), 1)
            self.assertIn("daily API budget cap", self.log())
            self.assertEqual(calls[0]["path"], "/v1/chat/completions")
            self.assertEqual(calls[0]["body"]["messages"][0]["content"], self.brief.read_text())
        row = next(iter(next(iter(self.ledger()["days"].values())).values()))
        self.assertEqual(Decimal(row["spent_usd"]), Decimal("0.002"))
        self.assertEqual((row["input_tokens"], row["output_tokens"]), (1000, 500))
        self.assertEqual(row["reservations"], {})

    def test_api_reservations_prevent_concurrent_overspend(self):
        with api_server(delay=0.3) as (url, calls):
            self.configure({"api": self.api(url, daily_budget_usd=0.002), "good": self.engine()})
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                futures = [pool.submit(self.run_relay, output=self.directory / f"api{i}.md") for i in range(4)]
                for future in futures:
                    future.result()
            self.assertEqual(len(calls), 1)

    def test_api_http_error_falls_back_and_releases_reservation(self):
        with api_server(status=429) as (url, calls):
            self.configure({"api": self.api(url), "good": self.engine()})
            self.run_relay()
            self.assertEqual(len(calls), 1)
            self.assertIn("HTTP 429", self.log())
        row = next(iter(next(iter(self.ledger()["days"].values())).values()))
        self.assertEqual(row["reservations"], {})

    def test_api_timeout_includes_slow_response_body(self):
        with api_server(drip=True) as (url, calls):
            self.configure({"api": self.api(url, timeout=0.35), "good": self.engine()})
            started = time.monotonic()
            self.run_relay()
            self.assertLess(time.monotonic() - started, 2)
            self.assertIn("engine timeout", self.log())
        row = next(iter(next(iter(self.ledger()["days"].values())).values()))
        self.assertTrue(row["reservations"])

    def test_api_redirect_is_not_followed(self):
        with api_server(status=302) as (url, calls):
            self.configure({"api": self.api(url), "good": self.engine()})
            self.run_relay()
            self.assertEqual(len(calls), 1)
            self.assertIn("HTTP 302", self.log())

    def test_invalid_usage_keeps_reservation(self):
        with api_server(response={"choices": [{"message": {"content": "Answer"}}]}) as (url, calls):
            self.configure({"api": self.api(url), "good": self.engine()})
            self.run_relay()
            self.assertIn("invalid API usage", self.log())
        row = next(iter(next(iter(self.ledger()["days"].values())).values()))
        self.assertTrue(row["reservations"])

    def test_api_truncation_is_charged_then_falls_back(self):
        response = {"choices": [{"message": {"content": "Partial"}, "finish_reason": "length"}],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 10}}
        with api_server(response=response) as (url, calls):
            self.configure({"api": self.api(url), "good": self.engine()})
            self.run_relay()
            self.assertIn("incomplete API answer", self.log())
        row = next(iter(next(iter(self.ledger()["days"].values())).values()))
        self.assertEqual(Decimal(row["spent_usd"]), Decimal("0.00004"))

    def test_missing_api_key_skips_network(self):
        with api_server() as (url, calls):
            self.configure({"api": self.api(url), "good": self.engine()})
            del self.environment["FAKE_API_KEY"]
            self.run_relay()
            self.assertEqual(calls, [])

    def test_corrupt_ledger_stops_api_without_resetting_costs(self):
        state = self.directory / "state"
        state.mkdir()
        (state / "costs.json").write_text("not JSON")
        with api_server() as (url, calls):
            self.configure({"api": self.api(url), "good": self.engine()})
            result = self.run_relay(expected=1)
            self.assertIn("Invalid cost ledger", result.stderr)
            self.assertEqual(calls, [])
        self.assertEqual((state / "costs.json").read_text(), "not JSON")

    def test_cost_ledger_lock_timeout(self):
        with api_server() as (url, calls):
            self.configure({"api": self.api(url)}, lock_timeout=0.1)
            with relay.file_lock(self.directory / "state" / "costs.lock", 1):
                result = self.run_relay(expected=1)
            self.assertIn("Lock wait timeout", result.stderr)
            self.assertEqual(calls, [])

    def test_budget_resets_at_utc_day_boundary(self):
        state = self.directory / "state"
        ledger = relay.Ledger(state, 1)
        state.mkdir()
        (state / "costs.json").write_text(json.dumps({"version": 1, "days": {
            "2000-01-01": {"api": {"spent_usd": "50", "reservations": {}}}}}))
        self.assertIsNotNone(ledger.reserve("api", Decimal("0.1"), Decimal("1")))

    def test_invalid_config_and_empty_brief(self):
        self.configure({"good": self.engine()}, max_concurrent=0)
        self.assertIn("max_concurrent", self.run_relay(expected=1).stderr)
        self.configure({"good": self.engine()})
        self.brief.write_text("  ")
        self.assertIn("Brief is empty", self.run_relay(expected=1).stderr)

    def test_output_cannot_overwrite_inputs(self):
        self.configure({"good": self.engine()})
        result = self.run_relay(output=self.brief, expected=1)
        self.assertIn("must not overwrite", result.stderr)
        self.assertIn("Check source.txt", self.brief.read_text())

    def test_example_config_loads(self):
        config = relay.load_config(ROOT / "examples" / "relay.toml")
        self.assertEqual(set(config["modes"]), set(relay.MODES))
        self.assertEqual(config["relay"]["max_concurrent"], 4)


if __name__ == "__main__":
    unittest.main()
