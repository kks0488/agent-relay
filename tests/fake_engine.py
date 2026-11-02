"""Deterministic local engine used by the integration tests."""

import argparse
import fcntl
import json
from pathlib import Path
import sys
import time

parser = argparse.ArgumentParser()
parser.add_argument("behavior")
parser.add_argument("--name", default="fake")
parser.add_argument("--trace")
parser.add_argument("--capture")
parser.add_argument("--output")
parser.add_argument("--argument")
parser.add_argument("--delay", type=float, default=0.35)
args = parser.parse_args()
prompt = sys.stdin.read()


def trace(event):
    if args.trace:
        with open(args.trace, "a", encoding="utf-8") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            stream.write(json.dumps({"name": args.name, "event": event, "time": time.monotonic()}) + "\n")
            stream.flush()


trace("start")
if args.capture:
    with open(args.capture, "a", encoding="utf-8") as stream:
        stream.write(json.dumps({"name": args.name, "prompt": prompt, "argument": args.argument}) + "\n")
answer = "A useful answer with evidence."
code = 0
if args.behavior == "failure":
    print("ERROR: process failed", file=sys.stderr)
    code = 7
elif args.behavior == "empty":
    answer = " \n\t"
elif args.behavior == "rate":
    answer = "ERROR: rate limit reached (429)"
elif args.behavior == "stderr-rate":
    print("ERROR: quota exceeded", file=sys.stderr)
elif args.behavior == "body":
    answer = "The error rate fell. A 429 response indicates a rate limit.\nThe quota test now passes."
elif args.behavior == "quoted":
    answer = "An example diagnostic:\n```text\nERROR: quota exceeded\n```"
elif args.behavior == "echo":
    answer = "The error report is valid."
    print(answer, file=sys.stderr)
elif args.behavior in ("private-output", "private-output-failure"):
    answer = "UNIQUE_FINAL_RESPONSE_응답"
    if args.output:
        print("UNIQUE_STDOUT_RESPONSE")
        print("UNIQUE_STDOUT_RESPONSE", file=sys.stderr)
    print(answer, file=sys.stderr)
    print("diagnostic preserved; token=private-diagnostic-secret", file=sys.stderr)
    if args.behavior == "private-output-failure":
        code = 7
elif args.behavior == "slow":
    time.sleep(args.delay)
elif args.behavior == "secrets":
    print("sk-" + "S" * 30, file=sys.stderr)
    print("ghp_" + "T" * 36, file=sys.stderr)
    print("b7" * 24, file=sys.stderr)
    print("Authorization: Bearer opaque-example-value", file=sys.stderr)
    print('token="opaque token value"', file=sys.stderr)
elif args.behavior == "long-secret":
    print("sk-" + "S" * 4500, file=sys.stderr)
elif args.behavior in ("cross", "cross-fail-comparison"):
    if "## Answer A" in prompt:
        if args.behavior == "cross-fail-comparison":
            trace("end")
            sys.exit(9)
        value = Path("source.txt").read_text().strip()
        answer = (f"Final answer\nThe value is {value}.\n\nComparison table\n"
                  f"| Item | Answer A | Answer B | Verified value | Evidence | Uncertainty |\n"
                  f"| --- | --- | --- | --- | --- | --- |\n"
                  f"| Value | 7 | 9 | {value} | source.txt:1 | none |")
    else:
        answer = "The value is " + ("7" if args.name == "alpha" else "9") + "."
if args.output:
    Path(args.output).write_text(answer, encoding="utf-8")
else:
    print(answer)
trace("end")
sys.exit(code)
