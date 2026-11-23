# Run evaluations with Harbor (experimental)

The [Harbor](https://www.harborframework.com/) backend lets agent-cost-bench compare coding agents on native Harbor tasks. Harbor sets up the sandbox, installs the agent, and runs the task's verifier; agent-cost-bench reports pass rate, cost, and latency across runners. Use existing Terminal-Bench task directories or bring Harbor-format tasks from your own repositories or task generators.

## Choose a backend

| | Host (default) | Harbor (experimental) |
|---|---|---|
| Tasks | Native `task.yaml` or tasks converted by the Terminal-Bench importer | Local single-step Harbor task directories; no Hub datasets or mixed host/Harbor runs |
| Modes | `cli-compare` and `model-compare` | `cli-compare` only |
| Agent setup | Install and authenticate each CLI on the host | Harbor installs agents in the sandbox; configure headless credentials |
| Supported CLIs | See the [root README](../../README.md#what-you-can-do) | Claude Code, OpenCode V2, Codex, GitHub Copilot, Cursor, Antigravity, Devin, and pi. Kiro and Qoder remain host-only. |

## Install

From the repository root, install the `harbor` extra in a Python 3.12+ virtual environment. The extra includes EC2 support:

```sh
python -m pip install -e '.[harbor]'
```

If you already use uv, `uv sync --locked --extra harbor` is an alternative; invoke `agent-cost-bench` through `uv run --locked --extra harbor`. Run from the repository root: relative task and output paths are resolved from the working directory, not the YAML file's directory.

## Run with local Docker

Docker is Harbor's default sandbox and requires a local Docker daemon and Compose v2. Finch and this project's `CONTAINER_RUNTIME` setting do not configure Harbor.

The [Docker example](config.cli-compare.harbor-docker.example.yaml) points at the bundled [`hello-file`](hello-file/) task. Validate it without starting a sandbox or making model calls:

```sh
agent-cost-bench cli-compare validate \
  examples/harbor/config.cli-compare.harbor-docker.example.yaml
```

For a first paid trial, export a scoped, temporary AWS role session as `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, and `AWS_SESSION_TOKEN`. The Docker example passes these standard SDK variables to the Bedrock runners; it does not mount your host AWS profile or use a Bedrock API key. Leave `AWS_PROFILE`, `AWS_BEARER_TOKEN_BEDROCK`, `OPENAI_BASE_URL`, and `OPENAI_API_BASE` unset. Confirm access to the configured Claude Opus 5.5 profile in `us-west-2`, then run:

```sh
agent-cost-bench cli-compare run \
  examples/harbor/config.cli-compare.harbor-docker.example.yaml --runner claude-code --repeats 1
```

To smoke-test all eight adapters, verify model access and export the credential variables used by the example, then omit `--runner` and use `--task hello-file --repeats 1`. The example runs those paid trials serially. The Docker matrix has offline test coverage; EC2 smoke runs have passed for Claude Code, Codex, OpenCode V2, and pi using their EC2-specific configurations.

- OpenCode V2 and pi use the US Geo `us.openai.gpt-6-sol` profile through Bedrock Runtime ConverseStream in `us-west-2`.
- Codex uses the same profile through Runtime Responses, with hosted web search disabled. The role session needs access to the US Geo profile, its underlying model, and the default Bedrock project for Responses API calls.

## Configure your own evaluation

- Set `execution.backend: harbor` and point `task_sources` at a local task directory or a directory of immediate task directories. Filter by directory name with `tasks` in the source or `--task` on the CLI.
- Set `execution.harbor.env` to a [Harbor sandbox](https://docs.harborframework.com/sandboxes/pre-integrated-sandboxes) available in the pinned SDK, or a trusted custom `module.path:ClassName`. Use `execution.harbor.kwargs` for provider settings and `execution.harbor.options` for common settings such as `override_cpus`.
- Harbor owns CLI invocation; do not copy host `cli_base_args`. Use the adapter's model ID format (OpenCode needs `provider/model`) and configure overrides under the runner's `harbor` block. Host login sessions are not mounted, but Harbor can forward known credentials. Use scoped environment references, not literal secrets.
- `validate` checks configuration offline. Before creating trials, `run` also checks selected agents' local settings and credential inputs using Harbor's preflight. This does not contact model providers, confirm access, or require local Docker for EC2.
- Use trusted task definitions and images. Harbor follows the task's network and verifier settings. The bundled task uses a separate verifier and passes only the output artifact to it.
- Repeats and concurrency still apply; `execution.harbor.n_concurrent` adds a global cap. Harbor uses task-defined timeouts unless the run or task source overrides them. Setup and verifier errors report `unavailable`; agent timeouts report `timeout`.
- Reports include reward and timing; agent time excludes sandbox setup and verification. Verifier transcripts stay in `output_dir/<run-id>.harbor/` alongside agent logs, trajectories, and any `ctrf.json`. Review those artifacts before sharing.
- Cost prefers native telemetry, then configured per-runner token rates when usage is available. The examples include US Geo Bedrock fallback rates; update them when changing profiles or billing assumptions. Missing cost is `null`, not zero. Cost per success divides all attempt costs by passes and is unavailable if any attempt cost is unknown. Estimates exclude infrastructure charges.

## Run on AWS EC2

Harbor provisions ephemeral EC2 workers and bootstraps Docker and Compose remotely; no local Docker is required. A separate verifier can launch a second worker. The controller needs AWS permissions to manage the instances, and the workers need an AMI compatible with the instance type, a subnet and security group, SSH access from the controller, egress for setup and model calls, and an instance profile scoped to the workload.

The [EC2 example](config.cli-compare.harbor-ec2.example.yaml) uses the worker's Bedrock instance role for all four runners. Claude Code uses Claude Opus 5.5; Codex, OpenCode V2, and pi use US Geo GPT‑6 Sol. Codex uses Runtime Responses; OpenCode V2 and pi use Runtime ConverseStream. Use existing infrastructure or the optional [CloudFormation template](ec2-infra.cfn.yaml). The template creates a single-AZ VPC, public subnet, worker security group (SSH from an explicit CIDR and outbound TCP 80/443), and Bedrock instance profile. It resolves a worker AMI and can generate an SSH key pair; it does not launch instances or create a NAT gateway. Use a sandbox account with an agreed budget and review your organization's network and IAM requirements before adapting this template beyond the sample.

### Prepare the stack and config

1. Choose an AWS Region and Availability Zone. Set `WorkerKeyPair` to an existing regional EC2
   key pair name, or leave it blank to generate one. Keep the local private key on the controller,
   with owner-only file permissions. Prefer the controller's public IPv4 `/32`
   for `ControllerSshCidr`. A wider IPv4 range is allowed, but `0.0.0.0/0` exposes SSH to the entire
   internet and is not recommended. There is no default. Use a current, trusted Ubuntu amd64 AMI
   with password SSH disabled. The stack resolves Canonical's regional Ubuntu 24.04 AMI by default;
   override `WorkerAmiParameter` to use an approved SSM AMI parameter. Set `instance_type` directly
   in the Harbor config to an x86-64 type available in the selected AZ.
   If overriding `VpcCidr` (`10.72.0.0/24`) or `WorkerSubnetCidr` (`10.72.0.0/26`), make sure the
   subnet fits within the VPC range.
2. Select supported models or inference profiles in the Harbor config and confirm model access
   in the account, including the Anthropic use-case setup if required. For cross-Region profiles,
   check account/SCP restrictions in all destination Regions. The sample uses
   `us.anthropic.claude-opus-5-5` and `us.openai.gpt-6-sol` from `us-west-2`.
3. If using the optional stack, deploy `examples/harbor/ec2-infra.cfn.yaml` in that Region with
   `CAPABILITY_IAM`. Whether or not you use the stack, use a separate controller
   identity with scoped EC2 launch/tag/describe/terminate permissions and `iam:PassRole` restricted
   to the `WorkerRoleArn` output and `iam:PassedToService=ec2.amazonaws.com`. The template does not
    grant these controller permissions. The worker role can invoke foundation models and the account's
    regional inference profiles. It can
   create responses on `bedrock-runtime` using the stack Region's default project, but cannot manage
   stored responses or launch infrastructure.
4. Copy the [EC2 example](config.cli-compare.harbor-ec2.example.yaml) to
   `examples/harbor/config.harbor-ec2.yaml` (Git-ignored). Fill in the stack outputs or equivalent
   values, including `ami_id` (`WorkerAmiId` from the stack), your local SSH key path, and the
   named launcher `aws_profile`. If changing Regions, update the runner regions and OpenCode's
   endpoint as well as the EC2 Region. If changing Claude's profile, update `model_id` and all
   three tier aliases to match.

### Download a generated key

If `WorkerKeyPair` was blank, CloudFormation creates an Ed25519 key pair and stores its private key
in an SSM SecureString. The stack outputs only the key name (`WorkerKeyName`) and parameter name
(`WorkerPrivateKeyParameterName`). The deployment identity needs EC2 key-pair management and
`ssm:PutParameter`/`ssm:DeleteParameter` permissions. Use an authorized identity with `ssm:GetParameter`
on that parameter to retrieve it; the worker role does not need access.

Set `KEY_PARAMETER` to the `WorkerPrivateKeyParameterName` output and `REGION` to the stack Region:

```sh
mkdir -p -m 700 "$HOME/.ssh"
(umask 077; set -C; aws ssm get-parameter --region "$REGION" --name "$KEY_PARAMETER" \
  --with-decryption --query 'Parameter.Value' --output text > "$HOME/.ssh/harbor-worker.pem")
```

Set `ssh_key_path` to that file only after the command succeeds. The command won't overwrite an
existing file. CloudFormation deletes its generated key pair and SSM parameter on stack deletion;
remove your local copy after the pilot. A supplied key pair is not managed by the stack.

### Bring your own network

With existing infrastructure, skip the stack and supply equivalent IDs, routing, and a scoped
worker profile. With `use_public_ip: false`, provide a subnet with private SSH connectivity from
the controller and egress for package setup, image pulls, CLI installation, and Bedrock. Omitting
the subnet is not a portable default: it relies on EC2 having usable default networking, and
Harbor requires an explicit subnet for an ephemeral worker with `use_public_ip: false`.

For the AMI, you can resolve Canonical's Ubuntu 24.04 amd64 image in your chosen Region:

```sh
aws ssm get-parameter --region "$REGION" \
  --name /aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id \
  --query 'Parameter.Value' --output text
```

### Check before a live EC2 run

- **Worker metadata and storage:** Harbor 0.27.0 does not pass EC2 metadata options, a launch
  template, or an EBS encryption flag to `RunInstances`. Verify that the AMI and account settings
  yield IMDS enabled, IMDSv2 required, a response hop limit of 2 for the Docker bridge, and encrypted
  root EBS. Do not change account-wide defaults for this pilot. If you cannot meet these settings,
  do not launch ephemeral workers with this config. Harbor can attach to a preconfigured worker
  (`launch_mode: attach`, `instance_id`); it does not terminate attached instances. Review the
  verifier-isolation requirements before using attach mode instead of the supplied ephemeral pilot.
- **Credentials:** The EC2 runners use the worker role through the AWS credential chain inside
  the container. Harbor can forward ambient AWS credentials. Use a local named launcher profile
  in `kwargs.aws_profile`, not exported AWS credentials or an exported `AWS_PROFILE`. Do not set
  API keys, AWS credentials, bearer tokens, or OAuth tokens in runner/task env or sandbox options.
  The worker role is reachable by code running in the worker, including trusted task code. A separate
  verifier may launch a second VM using the same environment settings and profile.
- **Network:** Public IPv4 is an explicit Harbor launch option, not a subnet default. SSH uses
  Harbor's `accept-new` host-key policy (trust on first use). For verified host keys or stricter
  egress and network logging controls, provision an approved private topology rather than reusing
  the public pilot stack.

From the repository root, validate offline:

```sh
agent-cost-bench cli-compare validate \
  examples/harbor/config.harbor-ec2.yaml
```

Only after approving scope and spending, run one trial with credential variables removed from the
process environment (the named EC2 launcher profile remains available to the controller):

```sh
env -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY -u AWS_SESSION_TOKEN -u AWS_PROFILE \
  -u AWS_BEARER_TOKEN_BEDROCK -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN \
  -u ANTHROPIC_BASE_URL -u CLAUDE_CODE_OAUTH_TOKEN -u CLAUDE_FORCE_OAUTH \
  -u OPENAI_BASE_URL -u OPENAI_API_BASE \
  agent-cost-bench cli-compare run \
  examples/harbor/config.harbor-ec2.yaml --runner claude-code-bedrock --repeats 1
```

Check for `reward: 1` and `sandbox: ec2` in the report. Verify that **all** trial instances tagged
`Project=harbor-bedrock-smoke` terminated, including the verifier and any workers left by an interrupted
run. Then delete the optional stack. EC2, EBS, public IPv4, data transfer, and Bedrock calls can incur
charges; concurrency is not a spending cap.

### Template security and lint

- Reviewed against secure-by-default CloudFormation, least-privilege VPC/IAM, and IMDS guidance.
  No secret values are stack inputs or outputs. The SSH source CIDR must be set explicitly. The worker
  role uses explicit invocation actions and resource-type wildcards for model selection; restrict those
  ARNs for deployments that need a model allowlist. Worker metadata, disk encryption, and model
  access are deployment prerequisites, not guarantees of this template.
- `cfn-lint` 1.61.0 passed in `us-west-2`, `us-east-1`, and `eu-central-1`. This is schema validation, not
  a claim that the same inference profile or instance type works in those Regions.
- `cfn_nag` 0.8.10 reported no failures and two **unsuppressed** warnings on the template alone. **W5:** TCP 80/443 egress to
  internet destinations supports the stock Ubuntu repositories, container registries, CLI installer,
  and public Bedrock endpoint; it is port-limited, not destination-limited. A production design
  should use approved package/image mirrors or an egress proxy, and a Bedrock Runtime VPC endpoint
  where supported. **W60:** the short-lived sample VPC does not provision Flow Logs; enable them
  through your account's logging baseline if required. Scanning with `ControllerSshCidr=0.0.0.0/0`
  also reports **W2** (internet-wide ingress) and **W9** (ingress broader than `/32`).

```sh
cfn-lint -t examples/harbor/ec2-infra.cfn.yaml -r us-west-2 us-east-1 eu-central-1
cfn_nag_scan --input-path examples/harbor/ec2-infra.cfn.yaml --fail-on-warnings
```
