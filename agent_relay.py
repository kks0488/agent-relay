#!/usr/bin/env python3
"""Route a brief through local CLIs or a Chat Completions compatible API."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import ipaddress
import json
import math
import multiprocessing
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import uuid

try:
    import fcntl
except ImportError:
    fcntl = None

MODES = ("research", "code", "review", "cross", "write")


class RelayError(Exception):
    """An actionable configuration or execution failure."""


class CapacityError(RelayError):
    """The bounded wait for a shared resource expired."""


def redact(value):
    text = str(value)
    text = re.sub(r"\b(?:sk-|ghp_|github_pat_)[A-Za-z0-9_-]+", "[REDACTED]", text)
    text = re.sub(r"\b[0-9a-fA-F]{32,}\b", "[REDACTED]", text)
    text = re.sub(r"(?i)(bearer\s+)\S+", r"\1[REDACTED]", text)
    text = re.sub(
        r'''(?ix)(["']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|secret)["']?\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;}]+)''',
        r"\1[REDACTED]", text,
    )
    # Environment values also cover tokens without a recognizable prefix.
    for name, secret in os.environ.items():
        if len(secret) >= 8 and re.search(r"(?i)key|token|secret|password", name):
            text = text.replace(secret, "[REDACTED]")
    return text


class RunLog:
    def __init__(self, path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = path.open("a", encoding="utf-8")

    def write(self, message):
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.stream.write(f"{stamp} {redact(message)}\n")
        self.stream.flush()

    def close(self):
        self.stream.close()


def number(value, name, minimum=0, integer=False):
    if (type(value) not in (int, float) or not math.isfinite(value)
            or value < minimum or (integer and type(value) is not int)):
        raise RelayError(f"{name} must be a finite {'integer' if integer else 'number'} >= {minimum}")
    return value


def money(value):
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0:
            raise InvalidOperation
        return result
    except (InvalidOperation, ValueError):
        raise RelayError("Budget and prices must be finite nonnegative numbers") from None


def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".relay-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def file_lock(path, timeout):
    if fcntl is None:
        raise RelayError("File locking requires a POSIX system (Linux or macOS)")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise CapacityError(f"Lock wait timeout after {timeout:g}s") from None
                time.sleep(min(0.02, max(0, deadline - time.monotonic())))
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


@contextmanager
def execution_slot(state, limit, timeout, log):
    if fcntl is None:
        raise RelayError("File locking requires a POSIX system (Linux or macOS)")
    directory = state / "slots"
    directory.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    log.write(f"waiting for execution slot; limit={limit} max_wait={timeout:g}s")
    acquired = None
    try:
        while acquired is None:
            for index in range(limit):
                stream = (directory / f"{index}.lock").open("a+b")
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = stream
                    break
                except BlockingIOError:
                    stream.close()
            if acquired is None:
                if time.monotonic() >= deadline:
                    raise CapacityError(f"Execution slot wait timeout after {timeout:g}s (limit={limit})")
                time.sleep(min(0.02, max(0, deadline - time.monotonic())))
        yield
    finally:
        if acquired is not None:
            fcntl.flock(acquired, fcntl.LOCK_UN)
            acquired.close()


def load_config(path):
    with path.open("rb") as stream:
        config = tomllib.load(stream)
    settings = config.setdefault("relay", {})
    if not isinstance(settings, dict):
        raise RelayError("[relay] must be a table")
    for key, default, minimum, integer in (
        ("max_concurrent", 4, 1, True), ("wait_timeout", 60, 0, False),
        ("timeout", 300, 0.001, False), ("lock_timeout", 10, 0, False),
    ):
        settings[key] = number(settings.get(key, default), key, minimum, integer)
    if settings.get("review_language", "en") not in ("en", "ko"):
        raise RelayError("review_language must be en or ko")
    state = settings.get("state_dir", "~/.local/state/agent-relay")
    if not isinstance(state, str) or not state:
        raise RelayError("state_dir must be a nonempty path")
    settings["state_dir"] = (path.parent / Path(state).expanduser()).resolve()
    engines = config.get("engines")
    modes = config.get("modes")
    if not isinstance(engines, dict) or not engines or not isinstance(modes, dict):
        raise RelayError("Configuration needs [engines.NAME] tables and a [modes] table")
    for mode, order in modes.items():
        if (mode not in MODES or not isinstance(order, list) or not order
                or any(not isinstance(name, str) or name not in engines for name in order)
                or len(set(order)) != len(order)):
            raise RelayError(f"Invalid engine order for mode {mode}")
        if mode == "cross" and len(order) < 2:
            raise RelayError("cross requires at least two distinct engine entries")
    for name, engine in engines.items():
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name) or not isinstance(engine, dict):
            raise RelayError("Engine names must use letters, digits, underscores or hyphens")
        engine["timeout"] = number(engine.get("timeout", settings["timeout"]), "engine timeout", 0.001)
        kind = engine.get("type", "cli")
        if kind == "cli":
            command = engine.get("command")
            if not isinstance(command, list) or not command or any(not isinstance(x, str) for x in command):
                raise RelayError(f"{name}: command must be a nonempty array of strings")
            if engine.get("output", "stdout") not in ("stdout", "file"):
                raise RelayError(f"{name}: output must be stdout or file")
            if engine.get("output") == "file" and not any("{output}" in x for x in command):
                raise RelayError(f"{name}: file output requires an {{output}} placeholder")
        elif kind == "api":
            for key in ("base_url", "model", "api_key_env"):
                if not isinstance(engine.get(key), str) or not engine[key]:
                    raise RelayError(f"{name}: {key} is required")
            url = urllib.parse.urlsplit(engine["base_url"])
            local = url.hostname == "localhost"
            try:
                local = local or ipaddress.ip_address(url.hostname).is_loopback
            except ValueError:
                pass
            if (not url.hostname or url.username or url.password or url.query or url.fragment
                    or (url.scheme != "https" and not (url.scheme == "http" and local))):
                raise RelayError(f"{name}: base_url needs HTTPS (HTTP is allowed on loopback only), without credentials or query")
            engine["max_tokens"] = number(engine.get("max_tokens", 2048), "max_tokens", 1, True)
            if engine.get("token_parameter", "max_tokens") not in ("max_tokens", "max_completion_tokens"):
                raise RelayError(f"{name}: unsupported token_parameter")
            for key in ("daily_budget_usd", "input_usd_per_million", "output_usd_per_million"):
                if key not in engine:
                    raise RelayError(f"{name}: {key} is required")
                engine[key] = money(engine[key])
        else:
            raise RelayError(f"{name}: type must be cli or api")
    return config


def terminal_error(text):
    """Only terminal diagnostic lines count, not mentions in the answer body."""
    lines = []
    fence = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            fence = not fence
            # A closing fence is content, so it must block an earlier diagnostic.
            lines.append("")
        elif stripped:
            lines.append("" if fence or stripped.startswith(">") else stripped)
    if not lines or not lines[-1]:
        return None
    line = lines[-1]
    line = re.sub(r"^\[?\d{4}-\d\d-\d\d[T ][\d:.+Z-]+\]?\s+", "", line)
    diagnostic = re.match(
        r"^(?:ERROR\b|\[ERROR\]|(?i:error:|fatal\b|stream error\b|AGY_ERROR\b|"
        r"RESOURCE_EXHAUSTED\b|HTTP(?:/\S+)?\s+[45]\d\d\b|429\b|"
        r"rate[ _-]*limit(?:ed|\s+exceeded|\s+reached)?\s*(?::|$)|"
        r"quota\s+(?:exceeded|exhausted)|authentication required\b))", line,
    )
    return line if diagnostic else None


def result_failure(answer, diagnostics=""):
    if not answer.strip():
        return "empty result"
    # Some CLIs echo the final answer on stderr. Do not classify that echo twice.
    error = terminal_error(diagnostics.replace(answer.strip(), "")) or terminal_error(answer)
    return f"terminal engine error: {error}" if error else None


def kill_process_group(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def cli_call(engine, prompt, mode, workdir):
    with tempfile.TemporaryDirectory(prefix="agent-relay-") as directory:
        brief = Path(directory) / "brief.md"
        output = Path(directory) / "answer.md"
        brief.write_text(prompt, encoding="utf-8")
        values = {"prompt": prompt, "brief": str(brief), "output": str(output),
                  "workdir": str(workdir), "mode": mode}
        command = []
        for item in engine["command"]:
            command.append(re.sub(r"\{(prompt|brief|output|workdir|mode)\}",
                                  lambda match: values[match[1]], item))
        # Regular files avoid an unbounded pipe wait if a detached descendant
        # keeps stdout or stderr open after the foreground process times out.
        with brief.open("rb") as source, tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            process = subprocess.Popen(command, cwd=workdir, stdin=source,
                                       stdout=stdout_file, stderr=stderr_file,
                                       start_new_session=True)
            timed_out = False
            try:
                process.wait(timeout=engine["timeout"])
            except subprocess.TimeoutExpired:
                timed_out = True
            finally:
                kill_process_group(process)
                process.wait()
            stdout_file.seek(0)
            stderr_file.seek(0)
            stdout, stderr = stdout_file.read(), stderr_file.read()
        if timed_out:
            return "", stderr.decode("utf-8", "replace"), "engine timeout"
        diagnostics = stderr.decode("utf-8", "replace")
        stdout_text = stdout.decode("utf-8", "replace")
        answer = stdout_text
        if engine.get("output") == "file":
            diagnostics = answer + "\n" + diagnostics
            answer = output.read_text(encoding="utf-8", errors="replace") if output.exists() else ""
        failure = f"exit code {process.returncode}" if process.returncode else result_failure(answer, diagnostics)
        if not failure:
            # Successful logs contain only stderr diagnostics, never stdout or
            # the final answer (some engines echo either body on stderr too).
            diagnostics = stderr.decode("utf-8", "replace")
            for body in (stdout_text.strip(), answer.strip()):
                if body:
                    diagnostics = diagnostics.replace(body, "[response omitted]")
        return answer, diagnostics, failure


class Ledger:
    def __init__(self, state, timeout):
        self.path = state / "costs.json"
        self.timeout = timeout

    @contextmanager
    def edit(self):
        with file_lock(self.path.with_suffix(".lock"), self.timeout):
            try:
                data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {"version": 1, "days": {}}
                if data.get("version") != 1 or not isinstance(data.get("days"), dict):
                    raise ValueError
                yield data
                atomic_write(self.path, json.dumps(data, indent=2) + "\n")
            except (ValueError, TypeError, AttributeError, KeyError):
                raise RelayError("Invalid cost ledger; restore it before using API engines") from None

    def reserve(self, key, amount, cap):
        day = datetime.now(timezone.utc).date().isoformat()
        identifier = uuid.uuid4().hex
        with self.edit() as data:
            row = data["days"].setdefault(day, {}).setdefault(key, {
                "spent_usd": "0", "reservations": {}, "input_tokens": 0, "output_tokens": 0,
            })
            used = money(row["spent_usd"]) + sum((money(x) for x in row["reservations"].values()), Decimal(0))
            if used + amount > cap or used >= cap:
                return None
            row["reservations"][identifier] = str(amount)
        return day, key, identifier

    def settle(self, reservation, cost, inputs=0, outputs=0):
        day, key, identifier = reservation
        with self.edit() as data:
            row = data["days"][day][key]
            del row["reservations"][identifier]
            row["spent_usd"] = str(money(row["spent_usd"]) + cost)
            row["input_tokens"] += inputs
            row["output_tokens"] += outputs


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http_worker(connection, url, key, body, timeout):
    """Keep the entire HTTP exchange inside a process with a hard deadline."""
    try:
        request = urllib.request.Request(url, data=body, headers={
            "Authorization": "Bearer " + key, "Content-Type": "application/json",
        })
        with urllib.request.build_opener(NoRedirect).open(request, timeout=timeout) as response:
            raw = response.read(16 * 1024 * 1024 + 1)
        if len(raw) > 16 * 1024 * 1024:
            connection.send(("transport", "response too large"))
        else:
            connection.send(("ok", raw))
    except urllib.error.HTTPError as error:
        connection.send(("http", error.code))
    except Exception as error:
        connection.send(("transport", type(error).__name__))
    finally:
        connection.close()


def http_call(url, key, body, timeout):
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=http_worker, args=(sender, url, key, body, timeout))
    try:
        process.start()
        sender.close()
        if not receiver.poll(timeout):
            return "transport", "engine timeout"
        try:
            return receiver.recv()
        except EOFError:
            return "transport", "HTTP worker exited without a response"
    finally:
        if process.pid is not None:
            if process.is_alive():
                process.terminate()
            process.join()
        sender.close()
        receiver.close()


def api_call(name, engine, prompt, ledger, log):
    key = os.environ.get(engine["api_key_env"])
    if not key:
        return "", "", "missing API key environment variable"
    payload = {"model": engine["model"], "messages": [{"role": "user", "content": prompt}],
               engine.get("token_parameter", "max_tokens"): engine["max_tokens"]}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    million = Decimal(1000000)
    estimate = ((len(body) + 1024) * engine["input_usd_per_million"]
                + engine["max_tokens"] * engine["output_usd_per_million"]) / million
    identity = engine["base_url"].rstrip("/") + "|" + engine["model"]
    budget_key = name + ":" + hashlib.sha256(identity.encode()).hexdigest()[:12]
    reservation = ledger.reserve(budget_key, estimate, engine["daily_budget_usd"])
    if reservation is None:
        return "", "", "daily API budget cap (including reservations)"
    log.write(f"engine={name} reserved_usd={estimate}")
    status, raw = http_call(engine["base_url"].rstrip("/") + "/chat/completions",
                            key, body, engine["timeout"])
    if status == "http":
        # A definite client rejection did not generate a completion. Other failures
        # may have consumed tokens, so retain the reservation until the UTC day ends.
        if 400 <= raw < 500 and raw != 408:
            ledger.settle(reservation, Decimal(0))
        return "", "", f"HTTP {raw}; uncertain charges remain reserved if applicable"
    if status != "ok":
        return "", "", f"API transport failure ({raw}); reservation retained"
    try:
        data = json.loads(raw)
        usage = data["usage"]
        inputs, outputs = usage["prompt_tokens"], usage["completion_tokens"]
        if type(inputs) is not int or type(outputs) is not int or min(inputs, outputs) < 0:
            raise ValueError
    except (ValueError, KeyError, TypeError):
        return "", "", "invalid API usage or JSON; reservation retained"
    cost = (inputs * engine["input_usd_per_million"] + outputs * engine["output_usd_per_million"]) / million
    ledger.settle(reservation, cost, inputs, outputs)
    log.write(f"engine={name} input_tokens={inputs} output_tokens={outputs} cost_usd={cost}")
    try:
        choice = data["choices"][0]
        answer = choice["message"]["content"]
        if not isinstance(answer, str):
            raise ValueError
        if choice.get("finish_reason") in ("length", "content_filter", "tool_calls"):
            return "", "", "incomplete API answer: " + choice["finish_reason"]
    except (ValueError, KeyError, TypeError, IndexError):
        return "", "", "invalid API answer"
    return answer, "", result_failure(answer)


class Relay:
    def __init__(self, config, workdir, log):
        self.config = config
        self.settings = config["relay"]
        self.workdir = workdir
        self.log = log
        self.ledger = Ledger(self.settings["state_dir"], self.settings["lock_timeout"])

    def run_chain(self, order, prompt, mode, stage):
        for name in order:
            engine = self.config["engines"][name]
            started = time.monotonic()
            self.log.write(f"stage={stage} engine={name} starting")
            try:
                with execution_slot(self.settings["state_dir"], self.settings["max_concurrent"],
                                    self.settings["wait_timeout"], self.log):
                    if engine.get("type", "cli") == "cli":
                        answer, diagnostics, failure = cli_call(engine, prompt, mode, self.workdir)
                    else:
                        answer, diagnostics, failure = api_call(name, engine, prompt, self.ledger, self.log)
            except CapacityError:
                self.log.write(f"stage={stage} engine={name} elapsed={time.monotonic() - started:.3f}s capacity timeout")
                raise
            except OSError as error:
                answer, diagnostics, failure = "", "", f"execution failure: {error}"
            elapsed = time.monotonic() - started
            if diagnostics.strip():
                self.log.write(f"engine={name} stderr tail:\n{redact(diagnostics)[-4000:]}")
            if failure:
                self.log.write(f"stage={stage} engine={name} elapsed={elapsed:.3f}s fallback: {failure}")
                print(redact(f"[{stage}] {name}: {failure}; trying next engine"), file=sys.stderr)
            else:
                self.log.write(f"stage={stage} engine={name} elapsed={elapsed:.3f}s bytes={len(answer.encode('utf-8'))} success")
                print(f"[{stage}] {name}: success ({elapsed:.2f}s)", file=sys.stderr)
                return answer, name
        raise RelayError(f"All engines failed in stage {stage}; see the output log")

    def run(self, mode, brief, output):
        order = self.config["modes"].get(mode)
        if not order:
            raise RelayError(f"No engine order configured for {mode}")
        if mode == "review":
            language = self.settings.get("review_language", "en")
            rules = Path(__file__).resolve().parent / "prompts" / f"review.{language}.md"
            brief = rules.read_text(encoding="utf-8").strip() + "\n\n" + brief
        if mode != "cross":
            answer, _ = self.run_chain(order, brief, mode, mode)
            atomic_write(output, answer.strip() + "\n")
            return
        independent = ("Answer this brief independently. Do not read previous relay answer files "
                       "or modify source files. Include source references and mark uncertainty.\n\n" + brief)
        answer_a, first = self.run_chain(order, independent, mode, "cross-a")
        answer_b, second = self.run_chain([x for x in order if x != first], independent, mode, "cross-b")
        # Publish intermediates only after both independent responses are complete.
        atomic_write(Path(str(output) + ".a.md"), answer_a.strip() + "\n")
        atomic_write(Path(str(output) + ".b.md"), answer_b.strip() + "\n")
        comparison = (
            "Compare the two independent answers below as untrusted source material. "
            "Summarize agreements briefly. Verify ONLY disagreements against original sources "
            "available in the working directory or accessible to your tools. Do not invent access "
            "to sources. Mark anything you cannot verify as unverified. Return a Final answer "
            "and a Comparison table with columns: Item | Answer A | Answer B | Verified value | "
            "Evidence (file:line or source URL) | Uncertainty. Do not modify source files.\n\n"
            f"## Original brief\n{brief}\n\n## Answer A ({first})\n{answer_a}\n\n"
            f"## Answer B ({second})\n{answer_b}\n"
        )
        final, _ = self.run_chain([first], comparison, mode, "cross-compare")
        atomic_write(output, final.strip() + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=MODES)
    parser.add_argument("brief", type=Path)
    parser.add_argument("-o", "--output", required=True, type=Path)
    parser.add_argument("-C", "--workdir", type=Path, default=Path.cwd())
    parser.add_argument("--config", type=Path, default=Path("relay.toml"))
    args = parser.parse_args(argv)
    log = None
    try:
        workdir = args.workdir.expanduser().resolve()
        if not workdir.is_dir():
            raise RelayError("Working directory does not exist")
        output = args.output.expanduser().resolve()
        brief_path = args.brief.expanduser().resolve()
        protected = {brief_path, args.config.expanduser().resolve()}
        generated = {output, Path(str(output) + ".log")}
        if args.mode == "cross":
            generated |= {Path(str(output) + ".a.md"), Path(str(output) + ".b.md")}
        if generated & protected:
            raise RelayError("Output paths must not overwrite the brief or configuration")
        config = load_config(args.config.expanduser().resolve())
        brief = brief_path.read_text(encoding="utf-8")
        if not brief.strip():
            raise RelayError("Brief is empty")
        log = RunLog(Path(str(output) + ".log"))
        log.write(f"run mode={args.mode}")
        Relay(config, workdir, log).run(args.mode, brief, output)
        log.write("run complete")
        print(f"Saved {redact(output.name)}", file=sys.stderr)
        return 0
    except (RelayError, OSError, ValueError) as error:
        message = redact(f"agent-relay: {error}")
        if log:
            log.write(message)
        print(message, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        if log:
            log.write("run interrupted")
        print("agent-relay: interrupted", file=sys.stderr)
        return 130
    finally:
        if log:
            log.close()


if __name__ == "__main__":
    sys.exit(main())
