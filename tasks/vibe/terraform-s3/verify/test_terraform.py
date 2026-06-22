"""
Harness tests for task-004-terraform-s3.

Parses every *.tf file in the workspace with python-hcl2 and checks for a
secure S3 bucket configuration. Tests are tolerant of structural variation —
they search recursively for meaningful values so correct HCL written in
slightly different (but valid) forms still passes.

WORKSPACE is set to the model's output directory by the harness.
"""

from __future__ import annotations

import os
from pathlib import Path

import hcl2
import pytest

WS = Path(os.environ.get("WORKSPACE", "."))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_tf():
    merged: dict = {"resource": [], "variable": [], "output": [], "terraform": []}
    for tf in WS.glob("*.tf"):
        try:
            with open(tf) as f:
                parsed = hcl2.load(f)
            for key in merged:
                if key in parsed:
                    merged[key].extend(parsed[key])
        except Exception:
            continue
    return merged


def _resources_of_type(parsed: dict, rtype: str) -> list:
    out = []
    for block in parsed.get("resource", []):
        if rtype in block:
            for _name, attrs in block[rtype].items():
                out.append(attrs)
    return out


def _deep_find(obj, key):
    """Yield every value stored under `key` anywhere in a nested dict/list."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                yield v
            yield from _deep_find(v, key)
    elif isinstance(obj, list):
        for item in obj:
            yield from _deep_find(item, key)


def _truthy(v) -> bool:
    return v is True or (isinstance(v, str) and v.strip().lower() == "true")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def tf():
    files = list(WS.glob("*.tf"))
    if not files:
        pytest.skip("no .tf files produced")
    return _load_tf()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_tf_files_exist():
    assert list(WS.glob("*.tf")), "no .tf files produced"


def test_has_s3_bucket(tf):
    assert _resources_of_type(tf, "aws_s3_bucket"), "aws_s3_bucket resource missing"


def test_has_versioning_resource(tf):
    assert _resources_of_type(tf, "aws_s3_bucket_versioning"), \
        "aws_s3_bucket_versioning resource missing"


def test_versioning_status_enabled(tf):
    vers = _resources_of_type(tf, "aws_s3_bucket_versioning")
    statuses = list(_deep_find(vers, "status"))
    assert any(isinstance(s, str) and s.strip().lower() == "enabled" for s in statuses), \
        "versioning status is not 'Enabled'"


def test_has_encryption_resource(tf):
    assert _resources_of_type(tf, "aws_s3_bucket_server_side_encryption_configuration"), \
        "aws_s3_bucket_server_side_encryption_configuration resource missing"


def test_encryption_is_aes256(tf):
    enc = _resources_of_type(tf, "aws_s3_bucket_server_side_encryption_configuration")
    algos = list(_deep_find(enc, "sse_algorithm"))
    assert any(isinstance(a, str) and a.strip().upper() == "AES256" for a in algos), \
        "sse_algorithm is not AES256"


def test_has_public_access_block_resource(tf):
    assert _resources_of_type(tf, "aws_s3_bucket_public_access_block"), \
        "aws_s3_bucket_public_access_block resource missing"


@pytest.mark.parametrize("flag", [
    "block_public_acls",
    "block_public_policy",
    "ignore_public_acls",
    "restrict_public_buckets",
])
def test_public_access_flag_is_true(tf, flag):
    pab = _resources_of_type(tf, "aws_s3_bucket_public_access_block")
    vals = list(_deep_find(pab, flag))
    assert any(_truthy(v) for v in vals), f"{flag} is not set to true"


def test_bucket_name_variable(tf):
    var_names = {name for block in tf.get("variable", []) for name in block.keys()}
    assert "bucket_name" in var_names, "variable 'bucket_name' missing"


def test_bucket_arn_output(tf):
    out_names = {name for block in tf.get("output", []) for name in block.keys()}
    assert "bucket_arn" in out_names, "output 'bucket_arn' missing"
