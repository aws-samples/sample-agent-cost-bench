"""Tests for the external-benchmark task importer (Terminal-Bench)."""

from __future__ import annotations

import pytest
import yaml

from agent_cost_bench.importers import import_task_source, list_importers
from agent_cost_bench.importers.base import ImportError_, slugify


def _make_tb_task(root, name="hello", with_image=True):
    d = root / name
    (d / "tests").mkdir(parents=True)
    (d / "environment").mkdir(parents=True)
    toml = 'version = "1.0"\n[agent]\ntimeout_sec = 300\n[verifier]\ntimeout_sec = 120\n'
    if with_image:
        toml += '[environment]\ndocker_image = "ubuntu:22.04"\n'
    (d / "task.toml").write_text(toml)
    (d / "instruction.md").write_text("# Title\n\nDo the thing.\n")
    (d / "tests" / "test.sh").write_text("echo 1 > /logs/verifier/reward.txt\n")
    (d / "environment" / "Dockerfile").write_text("FROM ubuntu:22.04\n")
    (d / "solution").mkdir()
    (d / "solution" / "solve.sh").write_text("true\n")
    return d


def test_list_importers_includes_supported_types():
    names = list_importers()
    assert "terminal-bench" in names


def test_slugify():
    assert slugify("Hello World!") == "hello-world"
    assert slugify("a__b  c") == "a-b-c"


def test_import_terminal_bench_directory_of_tasks(tmp_path):
    src = tmp_path / "tb"
    src.mkdir()
    _make_tb_task(src, "hello")
    _make_tb_task(src, "world")
    dest = tmp_path / "out"

    recs = import_task_source(source_type="terminal-bench", source_path=src, dest_root=dest)
    assert {r.source_id for r in recs} == {"hello", "world"}

    ty = yaml.safe_load((recs[0].dest_dir / "task.yaml").read_text())
    assert ty["mode"] == "vibe"
    assert ty["verify"]["parser"] == "reward-file"
    assert ty["verify"]["image"] == "ubuntu:22.04"
    assert ty["verify"]["timeout_seconds"] == 120
    # 300s agent timeout -> 5 minutes
    assert ty["timeout_minutes"] == 5
    # Instruction paragraph breaks survive the round-trip.
    assert "\n\n" in ty["prompt"]
    assert "src/" in ty["prompt"]
    # Authoritative tests are copied for read-only mounting.
    assert (recs[0].dest_dir / "verify" / "tests" / "test.sh").is_file()


def test_import_terminal_bench_single_task(tmp_path):
    src = tmp_path / "tb"
    src.mkdir()
    _make_tb_task(src, "solo")
    dest = tmp_path / "out"
    # Point directly at the task dir (not a dir-of-dirs).
    recs = import_task_source(source_type="terminal-bench", source_path=src / "solo", dest_root=dest)
    assert len(recs) == 1 and recs[0].source_id == "solo"


def test_import_terminal_bench_name_filter(tmp_path):
    src = tmp_path / "tb"
    src.mkdir()
    _make_tb_task(src, "a")
    _make_tb_task(src, "b")
    recs = import_task_source(
        source_type="terminal-bench", source_path=src, dest_root=tmp_path / "out", only=["b"]
    )
    assert [r.source_id for r in recs] == ["b"]


def test_import_missing_named_task_raises(tmp_path):
    src = tmp_path / "tb"
    src.mkdir()
    _make_tb_task(src, "a")
    with pytest.raises(ImportError_):
        import_task_source(
            source_type="terminal-bench", source_path=src,
            dest_root=tmp_path / "out", only=["nope"],
        )


def test_import_dockerfile_only_task_sets_build_context(tmp_path):
    src = tmp_path / "tb"
    src.mkdir()
    _make_tb_task(src, "nodkr", with_image=False)
    recs = import_task_source(
        source_type="terminal-bench", source_path=src, dest_root=tmp_path / "out"
    )
    # A local tag is derived and the framework is told how to build it (no
    # manual-build warning any more — the image is built on demand).
    assert recs[0].image == "tb-nodkr:imported"
    assert recs[0].warnings == []
    assert (recs[0].dest_dir / "verify" / "environment" / "Dockerfile").is_file()
    ty = yaml.safe_load((recs[0].dest_dir / "task.yaml").read_text())
    assert ty["verify"]["build_context"] == "verify/environment"


def test_import_task_with_prebuilt_image_has_no_build_context(tmp_path):
    src = tmp_path / "tb"
    src.mkdir()
    _make_tb_task(src, "prebuilt", with_image=True)
    recs = import_task_source(
        source_type="terminal-bench", source_path=src, dest_root=tmp_path / "out"
    )
    ty = yaml.safe_load((recs[0].dest_dir / "task.yaml").read_text())
    assert ty["verify"]["image"] == "ubuntu:22.04"
    # A named prebuilt image is used as-is; nothing to build.
    assert "build_context" not in ty["verify"]


def test_unknown_source_type_raises(tmp_path):
    with pytest.raises(ImportError_):
        import_task_source(source_type="bogus", source_path=tmp_path, dest_root=tmp_path / "o")


def test_terminal_bench_seeds_dockerfile_copy_inputs(tmp_path):
    """Files COPY'd into the image are seeded to inputs/; non-COPY'd files
    (e.g. a reference solution) are not, and the prompt gains a note."""
    d = tmp_path / "src" / "compress"
    (d / "tests").mkdir(parents=True)
    (d / "environment").mkdir(parents=True)
    (d / "task.toml").write_text(
        'version = "1.0"\n[agent]\ntimeout_sec = 300\n'
        '[environment]\ndocker_image = "ubuntu:24.04"\n'
    )
    (d / "instruction.md").write_text("Read /app/decomp.c and /app/data.txt.\n")
    (d / "tests" / "test.sh").write_text("echo 1 > /logs/verifier/reward.txt\n")
    # Dockerfile COPYs two inputs; main.rs (reference impl) is present but NOT copied.
    (d / "environment" / "Dockerfile").write_text(
        "FROM ubuntu:24.04\nWORKDIR /app\n"
        "COPY decomp.c /app\nCOPY data.txt /app\n"
    )
    (d / "environment" / "decomp.c").write_text("int main(){return 0;}\n")
    (d / "environment" / "data.txt").write_text("hello world\n")
    (d / "environment" / "main.rs").write_text("// reference solution, must NOT leak\n")

    imported = import_task_source(
        source_type="terminal-bench",
        source_path=str(tmp_path / "src"),
        dest_root=str(tmp_path / "out"),
    )
    assert len(imported) == 1
    dest = imported[0].dest_dir

    inputs = {p.name for p in (dest / "inputs").iterdir()} if (dest / "inputs").is_dir() else set()
    assert inputs == {"decomp.c", "data.txt"}
    assert "main.rs" not in inputs  # reference solution not leaked

    task = yaml.safe_load((dest / "task.yaml").read_text())
    assert "already been placed in your workspace" in task["prompt"]
    assert "do NOT search the filesystem" in task["prompt"]


def test_terminal_bench_no_inputs_when_no_copy(tmp_path):
    """A WORKDIR-only Dockerfile (no COPY) seeds no inputs and adds no note."""
    d = tmp_path / "src" / "noinput"
    (d / "tests").mkdir(parents=True)
    (d / "environment").mkdir(parents=True)
    (d / "task.toml").write_text(
        'version = "1.0"\n[environment]\ndocker_image = "python:3.13-slim"\n'
    )
    (d / "instruction.md").write_text("Write a script.\n")
    (d / "tests" / "test.sh").write_text("echo 1 > /logs/verifier/reward.txt\n")
    (d / "environment" / "Dockerfile").write_text("FROM python:3.13-slim\nWORKDIR /app\n")

    imported = import_task_source(
        source_type="terminal-bench",
        source_path=str(tmp_path / "src"),
        dest_root=str(tmp_path / "out"),
    )
    dest = imported[0].dest_dir
    assert not (dest / "inputs").exists()
    task = yaml.safe_load((dest / "task.yaml").read_text())
    assert "already been placed in your workspace" not in task["prompt"]


def _make_tb4_task(root, name="tb4", wheels=()):
    """A minimal Terminal-Bench 4.x task with a separate verifier image."""
    d = root / name
    (d / "tests").mkdir(parents=True)
    (d / "environment").mkdir(parents=True)
    (d / "task.toml").write_text(
        'artifacts = ["/app/filter.py"]\n'
        '[verifier]\ntimeout_sec = 120\nenvironment_mode = "separate"\n'
        '[agent]\ntimeout_sec = 300\n[environment]\ngpus = 0\n'
    )
    (d / "instruction.md").write_text("Create /app/filter.py.\n")
    (d / "tests" / "test.sh").write_text("echo 1 > /logs/verifier/reward.txt\n")
    (d / "tests" / "Dockerfile").write_text("FROM python:3.12-slim\nCOPY . /tests/\n")
    for w in wheels:
        (d / "tests" / "wheels").mkdir(exist_ok=True)
        (d / "tests" / "wheels" / w).write_bytes(b"")
    (d / "environment" / "Dockerfile").write_text("FROM python:3.12-slim\nWORKDIR /app\n")
    return d


def test_tb4_task_without_inputs_still_gets_src(tmp_path):
    """A task that ships no input files (html-js-filter) still gives the agent
    an empty src/ (= /app), so grading never stops at "No src/ in workspace"."""
    _make_tb4_task(tmp_path / "tb")
    recs = import_task_source(
        source_type="terminal-bench", source_path=tmp_path / "tb", dest_root=tmp_path / "out"
    )
    assert (recs[0].dest_dir / "src").is_dir()
    ty = yaml.safe_load((recs[0].dest_dir / "task.yaml").read_text())
    assert "platform" not in ty["verify"]


def test_tb4_x86_only_wheels_force_amd64_grading(tmp_path):
    """A grader that bundles x86_64-only wheels is built and run as linux/amd64
    (it crashes on an ARM Docker host otherwise), under its own image tag."""
    _make_tb4_task(tmp_path / "tb", wheels=[
        "numpy-2.1.3-cp312-cp312-manylinux_2_17_x86_64.manylinux2014_x86_64.whl",
    ])
    recs = import_task_source(
        source_type="terminal-bench", source_path=tmp_path / "tb", dest_root=tmp_path / "out"
    )
    v = yaml.safe_load((recs[0].dest_dir / "task.yaml").read_text())["verify"]
    assert v["platform"] == "linux/amd64"
    assert v["image"].endswith("-amd64")


def test_tb4_multiarch_wheels_keep_native_grading(tmp_path):
    _make_tb4_task(tmp_path / "tb", wheels=[
        "numpy-2.1.3-cp312-cp312-manylinux_2_17_x86_64.whl",
        "numpy-2.1.3-cp312-cp312-manylinux_2_17_aarch64.whl",
    ])
    recs = import_task_source(
        source_type="terminal-bench", source_path=tmp_path / "tb", dest_root=tmp_path / "out"
    )
    v = yaml.safe_load((recs[0].dest_dir / "task.yaml").read_text())["verify"]
    assert "platform" not in v


def _tb4_with_env(root, name, dockerfile, toml_extra=""):
    """A TB 4.x task (separate verifier) with a custom agent Dockerfile."""
    d = _make_tb4_task(root, name)
    (d / "environment" / "Dockerfile").write_text(dockerfile)
    if toml_extra:
        (d / "task.toml").write_text((d / "task.toml").read_text() + toml_extra)
    return d


@pytest.mark.parametrize("dockerfile", [
    # Inputs copied from an earlier build stage (mp-checkpoint-consolidation).
    "FROM python:3.11 AS builder\nWORKDIR /app\nRUN echo x > /app/checkpoints\n"
    "FROM python:3.11\nCOPY --from=builder /app/checkpoints /app/checkpoints\n",
    # Inputs generated by a script while the image is built (pretrain-shard-corruption).
    "FROM python:3.11\nWORKDIR /app\nCOPY gen.py /app/gen.py\n"
    "RUN python /app/gen.py && rm /app/gen.py\n",
])
def test_tb4_build_time_inputs_are_skipped(tmp_path, dockerfile):
    """The workspace can only be seeded from COPY lines, so a task whose /app
    is filled in by a build step would start the agent without its inputs."""
    _tb4_with_env(tmp_path / "tb", "gen", dockerfile)
    recs = import_task_source(
        source_type="terminal-bench", source_path=tmp_path / "tb", dest_root=tmp_path / "out"
    )
    assert not (recs[0].dest_dir / "task.yaml").exists()
    assert "created while the task image is built" in recs[0].warnings[0]


def test_tb4_plain_copy_and_installs_are_not_skipped(tmp_path):
    """COPY'd inputs, package installs and an external uv stage are fine."""
    _tb4_with_env(
        tmp_path / "tb", "ok",
        "FROM python:3.12-slim\n"
        "COPY --from=ghcr.io/astral-sh/uv@sha256:abc /uv /uvx /bin/\n"
        "RUN apt-get update && apt-get install -y git && pip install numpy\n"
        "WORKDIR /app\nCOPY data /app/data\nRUN gunzip /app/data/x.gz\n",
    )
    recs = import_task_source(
        source_type="terminal-bench", source_path=tmp_path / "tb", dest_root=tmp_path / "out"
    )
    assert (recs[0].dest_dir / "task.yaml").exists()


def test_tb4_verifier_collect_is_skipped(tmp_path):
    """[[verifier.collect]] runs a command in the agent's container (e.g. git
    diff into /tmp/agent.patch); there is none here, so it can't be graded."""
    _tb4_with_env(
        tmp_path / "tb", "collect", "FROM python:3.12-slim\nWORKDIR /app\n",
        '\n[[verifier.collect]]\ncommand = "git diff > /tmp/agent.patch"\n',
    )
    recs = import_task_source(
        source_type="terminal-bench", source_path=tmp_path / "tb", dest_root=tmp_path / "out"
    )
    assert not (recs[0].dest_dir / "task.yaml").exists()
    assert "agent's container" in recs[0].warnings[0]


def test_tb4_dedicated_verifier_skips_runtime_precheck(tmp_path):
    """A grader built from tests/Dockerfile isn't second-guessed by the
    missing-runtime pre-check: rs-archive-clone was wrongly dropped because a
    helper script it never runs mentions `cargo`."""
    from agent_cost_bench.config import _load_task_config
    from agent_cost_bench.preflight import unsupported_runtime_report

    d = _make_tb4_task(tmp_path / "tb", "rsclone")
    (d / "tests" / "generate_fixtures.py").write_text(
        'import subprocess\nsubprocess.run(["cargo", "build", "--release"])\n'
    )
    recs = import_task_source(
        source_type="terminal-bench", source_path=tmp_path / "tb", dest_root=tmp_path / "out"
    )
    task = _load_task_config(recs[0].dest_dir / "task.yaml")
    assert task.verify.dedicated_image is True
    assert unsupported_runtime_report([task]) == {}
