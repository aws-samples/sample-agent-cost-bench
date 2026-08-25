"""Grade the gateway-inference task.

70% live behaviour, 30% code structure, the same split as the other long job.

The arithmetic here is **functional verification that the gateway is wired**, not a
measurement of the model behind it. The questions have exact known answers, so a gateway
that routes correctly returns them and one that does not cannot fake it. Prose cannot
pass, and separators are stripped so "1,827,993" and "1827993" both count.

Three things this scorer is deliberately careful about, because each one has already
produced a wrong conclusion somewhere in this project:

* **Infra failure is not agent failure.** If the live probes die on throttling, quota,
  IAM propagation or expired credentials rather than on a wrong answer, the run is
  reported ``infra_suspected`` and should be treated as invalid rather than as "the model
  could not do it". A benchmark that scores an AWS control-plane delay as a model weakness
  produces a confidently wrong number.
* **Readiness, not a single shot.** Control-plane creates are eventually consistent, so
  live calls are retried with bounded backoff before counting as failures. Bounded, so a
  hanging implementation still fails.
* **Discovery, not a contract.** The prompt deliberately does not dictate file paths or
  flags, so this scorer finds what the agent built from ``git status`` and tries the
  plausible entrypoints. A discovery miss is its own checkpoint rather than being folded
  silently into "the code does not work".
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

QUESTIONS = [
    ("What is 8347 * 219? Reply with only the number.", "1827993"),
    ("What is 91205 - 47368? Reply with only the number.", "43837"),
]

# The harness caps verification at 300s regardless of the task's own timeout
# (agent_cost_bench/evaluator/functional.py: min(timeout_minutes * 60, 300)). The
# first version of this scorer had a retry budget far beyond that, so one run was
# killed mid-probe and recorded 0.00 with no checkpoints at all — a scorer failure
# scored as an agent failure, which is the exact mistake this task exists to avoid.
#
# So every live probe now runs against a deadline and the scorer degrades instead of
# dying: whatever it managed to check is reported, and a truncated run says so.
DEADLINE_S = 250          # leave the harness ~50s of headroom
LIVE_TIMEOUT = 55
ERROR_TIMEOUT = 40
RETRIES = 2
BACKOFF = 5
MAX_CANDIDATES = 4

_START = time.monotonic()


def budget_left():
    return DEADLINE_S - (time.monotonic() - _START)


def can_probe(cost=LIVE_TIMEOUT):
    """Only start a probe we can afford to finish."""
    return budget_left() > cost + 5

# Anything here means the failure came from the AWS side or from our own verify venv,
# not from the agent's code being wrong.
INFRA_MARKERS = (
    "expiredtoken", "credentials", "accessdenied", "unauthorized", "unrecognizedclient",
    "could not connect", "name or service not known", "temporary failure in name resolution",
    "throttl", "toomanyrequests", "endpointconnectionerror", "no such host",
    "serviceunavailable", "internalserver", "requestlimitexceeded",
    "quotaexceeded", "limitexceeded", "resourcenotready", "validationexception: role",
    "is not authorized to perform", "assumed-role", "iam",
    # our own pinned deps being wrong is an infra fault too: a pinned dependency that
    # lacked the symbol the agent correctly used once failed every arm.
    "modulenotfounderror", "importerror", "cannot import name",
    "no module named", "pip install",
)

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}

ARG_SHAPES = (
    lambda q: [q],
    lambda q: ["--ask", q],
    lambda q: ["-q", q],
    lambda q: ["--question", q],
    lambda q: ["--prompt", q],
)


def sh(args, timeout=60, cwd=None):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        return p.returncode, p.stdout or "", p.stderr or ""
    except subprocess.TimeoutExpired:
        return None, "", f"TIMEOUT after {timeout}s"
    except (FileNotFoundError, OSError) as e:
        return None, "", str(e)


def digits(s):
    return re.sub(r"[^0-9]", "", s or "")


def looks_like_infra(t):
    t = (t or "").lower()
    return any(m in t for m in INFRA_MARKERS)


def as_module(path):
    stem = path[:-3] if path.endswith(".py") else path
    return stem.replace(os.sep, ".")


def invocations(path):
    """Every way a delivered entrypoint might have to be started.

    This used to try Python only, from the workspace root only, which quietly
    decided the outcome of the whole task. This repository's existing gateway
    tooling is shell, the prompt tells the agent to follow the repository's own
    conventions, and one delivery therefore shipped no Python at all — so it had
    no candidate to try and failed a checkpoint it could not have passed. Another
    resolved its deployment state file relative to its own directory, so running
    it from the workspace root found nothing.

    Each invocation is a (argv, cwd) pair: script and module form for Python,
    direct and `bash` form for shell, run both from the repository root and from
    the file's own directory.
    """
    d = os.path.dirname(path) or "."
    if path.endswith(".py"):
        forms = ([sys.executable, path], [sys.executable, "-m", as_module(path)])
        local = ([sys.executable, os.path.basename(path)],)
    else:
        forms = ([path], ["bash", path])
        local = (["bash", os.path.basename(path)], ["./" + os.path.basename(path)])
    return [(f, None) for f in forms] + [(f, d) for f in local]


def changed_files():
    """What the agent added or modified, from git. Falls back to a walk."""
    out = set()
    rc, o, _ = sh(["git", "status", "--porcelain", "--untracked-files=all"])
    if rc == 0 and o.strip():
        for line in o.splitlines():
            path = line[3:].strip().strip('"')
            if path and not any(p in SKIP_DIRS for p in path.split(os.sep)):
                out.add(path)
    if not out:
        for root, dirs, files in os.walk("."):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for f in files:
                out.add(os.path.relpath(os.path.join(root, f), "."))
    return sorted(out)


changed = changed_files()
IS_TEST = lambda f: bool(re.search(r"(^|/)(tests?|conftest)|test_[^/]*\.py$|_test\.py$", f))
py_impl = [f for f in changed if f.endswith(".py") and os.path.isfile(f) and not IS_TEST(f)]
shell_files = [f for f in changed if f.endswith(".sh") and os.path.isfile(f)]

blob = ""
for f in py_impl + shell_files:
    try:
        blob += open(f, encoding="utf-8", errors="replace").read()
    except OSError:
        pass

# Same text with `#` comments removed. Checks that ask "did they actually do X"
# should not be satisfied by X being discussed in a comment. Crude but adequate:
# both Python and shell use `#`, and a `#` inside a string only costs us a false
# negative on that line, never a false positive.
code_only = "\n".join(l.split("#", 1)[0] for l in blob.splitlines())

cp: dict[str, dict] = {}

# ── structure, 30% ───────────────────────────────────────────────────────────
cp["inference_target"] = {
    # The whole point of the task: a Gateway target of type `inference`, not another
    # `mcp` one. targetConfiguration.inference is the shape the API accepts.
    "passed": bool(re.search(r'["\']?inference["\']?\s*[:=]', blob)
                   and re.search(r"create-gateway-target|create_gateway_target", blob)),
    "detail": "creates a gateway target of type inference",
}
cp["unique_names"] = {
    # Risk 2: a hardcoded gateway name makes concurrent arms delete each other.
    "passed": bool(re.search(
        r"uuid|RANDOM|date\s*\+%|\$\$|timestamp|short_id|suffix|uniq", blob, re.I)),
    "detail": "derives per-deployment resource names rather than hardcoding one",
}
cp["teardown_provided"] = {
    "passed": bool(re.search(r"delete-gateway|delete_gateway|delete-gateway-target|"
                             r"delete_gateway_target|delete-all", blob, re.I)),
    "detail": "teardown for what it created",
}
# A concrete model id ASSIGNED to something, not merely mentioned. The first
# version of this check required the id to follow the literal word "model" within
# four punctuation characters, so `INFERENCE_MODEL_ID="openai.gpt-oss-120b"` — the
# form the deliveries actually used — could never match, and the checkpoint failed
# on 100% of runs including the best ones. A check that never passes measures
# nothing; this one is validated against the recorded deliveries.
_MODEL_ASSIGN = re.compile(
    r"[A-Za-z_][\w]*model[\w]*\s*[:=]\s*[\"']?"
    r"((?:openai|anthropic|meta|mistral|amazon)\.[\w.:\-]+|gpt-[\w.\-]+|claude-[\w.\-]+)",
    re.I)
cp["model_pinned"] = {
    "passed": bool(_MODEL_ASSIGN.search(code_only)),
    "detail": "a specific model id is pinned in the delivered configuration",
}
cp["no_hardcoded_answers"] = {
    "passed": not any(a in digits(blob) for _q, a in QUESTIONS),
    "detail": "expected answers not embedded anywhere in the delivery",
}
cp["not_mocked"] = {
    "passed": not re.search(r"unittest\.mock|MagicMock|monkeypatch|responses\.add", blob),
    "detail": "the delivered path is not mocked",
}

# Validated against the nine recorded deliveries: every one of these passed except
# model_pinned, which only three failed. So five of the six are gates, not
# discriminators — they catch a delivery that skipped the point of the task, and
# contribute nothing between deliveries that did it. Kept for that reason, and the
# weighting says so: all six must pass for full structure marks, and the score is
# reported alongside how many of them actually separated anything.
SK = ["inference_target", "unique_names", "teardown_provided", "model_pinned",
      "no_hardcoded_answers", "not_mocked"]
structure = sum(1 for k in SK if cp[k]["passed"]) / len(SK)

# ── live behaviour, 70% ──────────────────────────────────────────────────────
# Rank candidate entrypoints: an explicit main guard first, then name hints.
def rank(path):
    try:
        src = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return (9, path)
    score = 5
    if "__main__" in src:
        score -= 2
    if re.search(r"argparse|sys\.argv|click|typer", src):
        score -= 2
    if re.search(r"gateway", os.path.basename(path), re.I):
        score -= 2
    if any(k in os.path.basename(path).lower() for k in ("cli", "main", "run", "ask", "invoke")):
        score -= 1
    return (score, path)


# Shell counts as a deliverable. Ask-shaped names first: an `ask`/`call`/`query`
# script is far likelier to answer a question than `deploy-all.sh`, and running a
# deploy script by accident costs real money.
def is_asker(p):
    b = os.path.basename(p).lower()
    return bool(re.search(r"ask|call|query|invoke|client|infer|chat|prompt", b))


def is_deployer(p):
    b = os.path.basename(p).lower()
    return bool(re.search(r"deploy|delete|destroy|teardown|setup|install|bootstrap", b))


def is_sourced(p):
    """Config/library files are meant to be sourced, not executed.

    Running one is not a harmless miss. `config.sh` in one delivery writes a
    resource suffix into the deployment state file when executed, so probing it
    fabricated a state file that had not existed — the scorer changed the very
    thing it was measuring.
    """
    b = os.path.basename(p).lower()
    return bool(re.search(r"^(config|conf|env|common|lib|util|shared|_)", b)
                or b in ("__init__.py", "settings.py", "constants.py"))


runnable = [f for f in (py_impl + shell_files)
            if not is_deployer(f) and not is_sourced(f)]
candidates = sorted(runnable, key=lambda p: (not is_asker(p), rank(p)))[:MAX_CANDIDATES]

# Read the deployment state BEFORE anything is executed, for the same reason.
_pre_state = sorted(
    os.path.join(r, n)
    for r, ds, ns in os.walk(".")
    if not any(p in SKIP_DIRS for p in r.split(os.sep))
    for n in ns
    if re.match(r"\.deployed[-_]state|deployment[-_]state", n)
)

q0, a0 = QUESTIONS[0]
entry = None
infra_hits = live_total = 0

# Why each candidate was rejected. Without this, "none answered" is the only
# record left and a later audit has to re-derive the cause from transcripts.
attempts: list[dict] = []

truncated = False
for path in candidates:
    if not can_probe():
        truncated = True
        break
    for base, cwd in invocations(path):
        if not can_probe():
            truncated = True
            break
        for shape in ARG_SHAPES:
            if not can_probe():
                truncated = True
                break
            # Retries exist for eventual consistency, so only an infra-shaped failure
            # earns one. Retrying a wrong answer or a bad flag just spends the
            # deadline: the search grid is candidates x invocation-forms x arg-shapes,
            # and retrying every cell took 88 probes and truncated the run, which
            # turns a measurement into a floor.
            for _attempt in range(RETRIES):
                if not can_probe():
                    truncated = True
                    break
                rc, out, err = sh([*base, *shape(q0)], LIVE_TIMEOUT, cwd=cwd)
                live_total += 1
                if looks_like_infra(err + out):
                    infra_hits += 1
                    attempts.append({"cmd": " ".join(base), "cwd": cwd or ".",
                                     "rc": rc, "why": "infra", "tail": (err or out)[-160:]})
                    time.sleep(BACKOFF)      # eventual consistency, not a wrong answer
                    continue
                if rc == 0 and a0 in digits(out):
                    entry = (base, shape, cwd)
                    break
                attempts.append({"cmd": " ".join(base), "cwd": cwd or ".", "rc": rc,
                                 "why": "no expected answer", "tail": (err or out)[-160:]})
                break
            if entry:
                break
        if entry:
            break
    if entry:
        break

if not candidates:
    detail = (f"no runnable entrypoint was delivered: {len(py_impl)} python and "
              f"{len(shell_files)} shell file(s) changed, none of them non-deploy")
elif entry:
    # Print the whole argv. Dropping argv[0] was fine while every entrypoint was
    # `python <file>`, but a shell script executed directly is a one-element argv, so
    # the field came out empty for exactly the deliveries this scorer was fixed to
    # support — and empty is what a reader checks first.
    detail = " ".join(entry[0]) + (f"  (cwd={entry[2]})" if entry[2] else "")
else:
    detail = (f"tried {len(candidates)} candidate(s) x script/module/bash x "
              f"root+own-dir, {live_total} invocation(s), none answered")

cp["entrypoint_found"] = {"passed": entry is not None, "detail": detail}

live_scores: list[float] = []
if not entry:
    for k in ("live_answers", "live_through_gateway"):
        cp[k] = {"passed": False, "detail": "skipped, no working entrypoint"}
    live_scores = [0.0, 0.0]
else:
    base, shape, cwd = entry
    got = 0
    for q, want in QUESTIONS:
        for _ in range(RETRIES):
            if not can_probe():
                truncated = True
                break
            rc, out, err = sh([*base, *shape(q)], LIVE_TIMEOUT, cwd=cwd)
            live_total += 1
            if looks_like_infra(err + out):
                infra_hits += 1
                time.sleep(BACKOFF)
                continue
            if rc == 0 and want in digits(out):
                got += 1
                break
    cp["live_answers"] = {"passed": got == len(QUESTIONS),
                          "detail": f"{got}/{len(QUESTIONS)} exact answers through the deployment"}
    live_scores.append(got / len(QUESTIONS))

    # Did the answer actually travel through a Gateway, or did the delivery quietly call
    # the model service directly? A direct call would answer correctly and prove nothing.
    rc, out, err = sh(["git", "diff", "--unified=0"], min(60, max(10, int(budget_left()))))
    diff = out + blob
    via_gateway = bool(re.search(r"gateway", diff, re.I)) and bool(
        re.search(r"bedrock-agentcore|agentcore", diff, re.I))
    cp["live_through_gateway"] = {
        "passed": via_gateway and got > 0,
        "detail": ("the answering path goes through an AgentCore Gateway"
                   if via_gateway else
                   "no evidence the answer travelled through a Gateway"),
    }
    live_scores.append(1.0 if (via_gateway and got > 0) else 0.0)

live = sum(live_scores) / len(live_scores)
score = round(0.70 * live + 0.30 * structure, 4)

# Risk 1: most probes dying on the AWS side means we measured AWS, not the agent.
infra_suspected = live_total > 0 and infra_hits >= max(1, live_total // 2)
budget_exhausted = truncated or budget_left() <= 0

# Whether the agent's deployment was still standing when the probes ran. The first
# version of this scorer probed only after the agent had finished, so an arm that
# tore down as the prompt asked had nothing left to answer and scored zero, while an
# arm that left billable gateways running scored full marks. Recording it makes that
# confound visible in the data instead of leaving it to be found by forensics later.
state_files = _pre_state
deployment_present = bool(state_files)

summary = (f"live {live:.0%} (70% weight), structure {structure:.0%} (30% weight)"
           + (f", entrypoint={' '.join(entry[0][1:])}" if entry else ", no entrypoint found")
           + ("  — VERIFICATION TRUNCATED: the 300s harness budget ran out before every "
              "probe finished, so the live score is a floor, not a measurement. Do not "
              "read it as an agent failure." if budget_exhausted else "")
           + ("  — INFRA SUSPECTED: most live probes failed on AWS-side errors "
              "(throttling / quota / IAM propagation / credentials) rather than on a wrong "
              "answer. Treat this run as invalid, not as an agent failure."
              if infra_suspected else ""))

print("AGENT_COST_BENCH_RESULT: " + json.dumps({
    "score": score,
    "checkpoints": cp,
    "summary": summary,
    "infra_suspected": infra_suspected,
    "verification_truncated": budget_exhausted,
    "seconds_used": round(time.monotonic() - _START, 1),
    "live_probes": live_total,
    "infra_hits": infra_hits,
    "changed_files": changed[:40],
    # Everything a later audit would otherwise have to reconstruct from transcripts.
    "candidates_tried": candidates,
    "py_impl_count": len(py_impl),
    "shell_count": len(shell_files),
    "attempts": attempts[:12],
    "deployment_state_present": deployment_present,
    "state_files": sorted(set(state_files))[:8],
    "acb_resource_prefix": os.environ.get("ACB_RESOURCE_PREFIX", "(unset)"),
}))
