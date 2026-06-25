"""
Harness tests for task-015-terraform-serverless-spa.

Runs inside the agent-cost-bench-terraform image, in the directory holding the model's
*.tf files (terraform init has already run offline against the pre-warmed AWS
provider mirror). Grading combines:

  * a real `terraform validate` (catches broken HCL, bad references, type and
    schema errors — the "stronger" signal), and
  * tolerant structural checks that the required architecture components and
    wiring are present.

Each test is one checkpoint; the functional score is the fraction that pass, so
partial implementations get partial credit. Structural checks are deliberately
forgiving of valid stylistic variation (they search concatenated, lower-cased
source plus parsed HCL).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import hcl2
import pytest

WS = Path(os.environ.get("WORKSPACE", "."))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tf_files() -> list[Path]:
    return sorted(WS.glob("*.tf"))


def _raw_lower() -> str:
    parts: list[str] = []
    for f in _tf_files():
        try:
            parts.append(f.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            pass
    return "\n".join(parts).lower()


def _load_tf() -> dict:
    merged: dict = {"resource": [], "variable": [], "output": [], "terraform": []}
    for tf in _tf_files():
        try:
            with open(tf) as f:
                parsed = hcl2.load(f)
            for key in merged:
                if key in parsed:
                    merged[key].extend(parsed[key])
        except Exception:
            continue
    return merged


def _has_resource(raw: str, rtype: str) -> bool:
    """True if a `resource "<rtype>"` block exists (raw text, parse-failure proof)."""
    return re.search(r'resource\s+"%s"' % re.escape(rtype), raw) is not None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def raw() -> str:
    if not _tf_files():
        pytest.skip("no .tf files produced")
    return _raw_lower()


@pytest.fixture(scope="module")
def tf() -> dict:
    if not _tf_files():
        pytest.skip("no .tf files produced")
    return _load_tf()


# ---------------------------------------------------------------------------
# Strong signal: terraform validate
# ---------------------------------------------------------------------------

def test_terraform_validate():
    """The configuration must be valid Terraform (init already ran offline)."""
    proc = subprocess.run(
        ["terraform", "validate", "-json"],
        capture_output=True, text=True,
    )
    detail = (proc.stdout or proc.stderr or "")[-800:]
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        assert proc.returncode == 0, f"terraform validate failed: {detail}"
        return
    assert data.get("valid") is True, (
        "terraform validate reported errors: "
        + "; ".join(d.get("summary", "") for d in data.get("diagnostics", []))[:600]
    )


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------

def test_files_exist():
    assert _tf_files(), "no .tf files produced"


def test_aws_provider_declared(raw):
    assert "hashicorp/aws" in raw or re.search(r'provider\s+"aws"', raw), \
        "AWS provider not declared (required_providers hashicorp/aws or provider \"aws\")"


# ---------------------------------------------------------------------------
# S3 SPA hosting
# ---------------------------------------------------------------------------

def test_has_s3_bucket(raw):
    assert _has_resource(raw, "aws_s3_bucket"), "aws_s3_bucket resource missing"


def test_s3_website_hosting(raw):
    assert _has_resource(raw, "aws_s3_bucket_website_configuration"), \
        "aws_s3_bucket_website_configuration missing (SPA static hosting)"


def test_s3_index_document(raw):
    assert "index.html" in raw, "website index document index.html not configured"


# ---------------------------------------------------------------------------
# DynamoDB
# ---------------------------------------------------------------------------

def test_has_dynamodb_table(raw):
    assert _has_resource(raw, "aws_dynamodb_table"), "aws_dynamodb_table resource missing"


def test_dynamodb_hash_key(raw):
    assert "hash_key" in raw, "DynamoDB table has no hash_key (partition key)"


# ---------------------------------------------------------------------------
# Lambda + IAM
# ---------------------------------------------------------------------------

def test_has_lambda_function(raw):
    assert _has_resource(raw, "aws_lambda_function"), "aws_lambda_function resource missing"


def test_has_iam_role(raw):
    assert _has_resource(raw, "aws_iam_role"), "aws_iam_role for Lambda missing"


def test_lambda_trust_policy(raw):
    assert "lambda.amazonaws.com" in raw, \
        "Lambda execution role does not trust lambda.amazonaws.com"


def test_iam_allows_dynamodb(raw):
    assert re.search(r"dynamodb:[a-z*]", raw) or "dynamodbfullaccess" in raw, \
        "IAM policy does not grant DynamoDB access"


def test_iam_allows_s3(raw):
    assert re.search(r"s3:[a-z*]", raw) or "amazons3fullaccess" in raw, \
        "IAM policy does not grant S3 access"


def test_lambda_env_wires_table_and_bucket(raw):
    assert (
        "environment" in raw
        and "aws_dynamodb_table" in raw
        and "aws_s3_bucket" in raw
    ), "Lambda environment does not pass the DynamoDB table and S3 bucket names"


# ---------------------------------------------------------------------------
# API Gateway
# ---------------------------------------------------------------------------

def test_has_api_gateway(raw):
    assert _has_resource(raw, "aws_apigatewayv2_api") or _has_resource(raw, "aws_api_gateway_rest_api"), \
        "No API Gateway (aws_apigatewayv2_api or aws_api_gateway_rest_api)"


def test_api_integrates_lambda(raw):
    has_integration = (
        _has_resource(raw, "aws_apigatewayv2_integration")
        or _has_resource(raw, "aws_api_gateway_integration")
    )
    refs_lambda = "invoke_arn" in raw or "aws_lambda_function" in raw
    assert has_integration and refs_lambda, \
        "API Gateway is not integrated with the Lambda function"


def test_lambda_permission_for_apigateway(raw):
    assert _has_resource(raw, "aws_lambda_permission") and "apigateway" in raw, \
        "Missing aws_lambda_permission allowing API Gateway to invoke Lambda"


# ---------------------------------------------------------------------------
# Route 53
# ---------------------------------------------------------------------------

def test_has_route53_record(raw):
    assert _has_resource(raw, "aws_route53_record"), "aws_route53_record resource missing"


def test_route53_targets_api(raw):
    assert _has_resource(raw, "aws_route53_record") and ("alias" in raw or "domain_name" in raw), \
        "Route 53 record does not route a domain to the API Gateway endpoint"


# ---------------------------------------------------------------------------
# Variables & outputs
# ---------------------------------------------------------------------------

def test_domain_variable(tf, raw):
    names = {n for b in tf.get("variable", []) for n in b.keys()}
    assert any("domain" in n.lower() for n in names) or re.search(r'variable\s+"[^"]*domain', raw), \
        "no domain-name variable found"


def test_outputs_present(tf, raw):
    names = {n.lower() for b in tf.get("output", []) for n in b.keys()}
    if names:
        api_out = any(re.search(r"api|endpoint|url|invoke", n) for n in names)
        s3_out = any(re.search(r"bucket|website|s3", n) for n in names)
    else:
        api_out = bool(re.search(r'output\s+"[^"]*(api|endpoint|url|invoke)', raw))
        s3_out = bool(re.search(r'output\s+"[^"]*(bucket|website|s3)', raw))
    assert api_out and s3_out, "missing outputs for the API endpoint and the S3 site/bucket"
