# Deploying search-sciops-bot to ECS Fargate

Slack Socket Mode only needs a persistent *outbound* WebSocket — no inbound
port, load balancer, or public URL. That simplifies this to: one Fargate
task, no ALB/target group, and a security group with no inbound rules at
all.

All commands assume `aws` CLI configured with sufficient permissions, and
are run from the repo root unless noted. Replace `<ACCOUNT_ID>` / `<REGION>`
throughout (or `sed` them in the JSON files under `deploy/`).

## 0. Prerequisites

- Set up goldie, tuner, then the orchestrator first, in that order (see
  `sciops/README.md`'s "Setup") and have the resulting `ORCHESTRATOR_ENV_ID` /
  `ORCHESTRATOR_AGENT_ID` / `ORCHESTRATOR_AGENT_VERSION` from `sciops/agents/orchestrator/.env` on hand.
- Create the Slack app (`sciops/slacker/slack_app_manifest.yaml`, see
  `sciops/slacker/README.md`) and have `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN` on hand.
- Have an `ANTHROPIC_API_KEY` with the Managed Agents / multi-agent beta enabled.

## 1. Build and push the image

```bash
aws ecr create-repository --repository-name search-sciops-bot --region <REGION>

aws ecr get-login-password --region <REGION> \
  | docker login --username AWS --password-stdin <ACCOUNT_ID>.dkr.ecr.<REGION>.amazonaws.com

docker build -f sciops/Dockerfile -t search-sciops-bot sciops
docker tag search-sciops-bot:latest <ACCOUNT_ID>.dkr.ecr.<REGION>.amazonaws.com/search-sciops-bot:latest
docker push <ACCOUNT_ID>.dkr.ecr.<REGION>.amazonaws.com/search-sciops-bot:latest
```

## 2. Store the three credentials in Secrets Manager

Only the actual credentials go in Secrets Manager — one secret per value,
under a common `search-sciops-bot/` prefix (matches the IAM policy in
`deploy/execution-role-secrets-policy.json`):

```bash
aws secretsmanager create-secret --name search-sciops-bot/slack-bot-token --secret-string "xoxb-..."
aws secretsmanager create-secret --name search-sciops-bot/slack-app-token --secret-string "xapp-..."
aws secretsmanager create-secret --name search-sciops-bot/anthropic-api-key --secret-string "sk-ant-..."
```

The three `ORCHESTRATOR_*` values are **not** secrets — they're opaque
agent/environment/file ids — so they live in the task definition's
`environment` block instead. That costs nothing (vs. ~$0.40/secret/month),
and makes the running agent version visible in the task definition rather
than hidden behind a secret. Fill them in from
`sciops/agents/orchestrator/.env` before registering in step 4;

ECS injects both blocks as plain environment variables in the container at
launch — the app reads everything from process env at startup (falling back
to local `sciops/slacker/.env` only outside ECS), so no code-level AWS SDK
calls are needed.

## 3. IAM: execution role

The **execution role** is used by the ECS agent itself to pull the image,
write logs, and fetch the secrets above — the app in the container gets no
AWS credentials at all (no task role is set, since the app makes no AWS API
calls of its own).

```bash
aws iam create-role --role-name search-sciops-bot-execution-role \
  --assume-role-policy-document file://sciops/slacker/deploy/execution-role-trust-policy.json

aws iam attach-role-policy --role-name search-sciops-bot-execution-role \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy

aws iam put-role-policy --role-name search-sciops-bot-execution-role \
  --policy-name search-sciops-bot-secrets \
  --policy-document file://sciops/slacker/deploy/execution-role-secrets-policy.json
```

## 4. Log group, cluster, task definition

```bash
aws logs create-log-group --log-group-name /ecs/search-sciops-bot --region <REGION>

# create-log-group defaults to never expiring — cap it, or pay to store this
# bot's logs forever.
aws logs put-retention-policy --log-group-name /ecs/search-sciops-bot \
  --retention-in-days 30 --region <REGION>

aws ecs create-cluster --cluster-name search-sciops-bot --region <REGION>

# fill in <ACCOUNT_ID>/<REGION> AND the three <ORCHESTRATOR_*> values in the
# file first (sed, or copy+edit)
aws ecs register-task-definition \
  --cli-input-json file://sciops/slacker/deploy/ecs-task-definition.json \
  --region <REGION>
```

The task is sized at `cpu: 256` / `memory: 1024`. vCPU is at the Fargate floor
because one WebSocket plus HTTP calls doesn't need more, and it's the largest
line item in the ~$16/month running cost (see `README.md`'s "Cost"). Memory is
1 GB rather than the 512 MB floor — $1.62/month more — because the bot buffers
whole files in memory and permits up to `MAX_CONCURRENT_THREADS` (25) threads
doing so, and ECS kills the task on OOM. Still worth glancing at
`MemoryUtilization` after the first real workload to confirm the headroom is
right.

**If the service already exists**, changing size means a new task definition
revision plus an explicit `update-service --task-definition
search-sciops-bot:<NEW_REVISION>` — `--force-new-deployment` alone re-runs the
revision the service is already pinned to and won't pick up the change.

## 5. Networking

No inbound traffic is needed. Simplest setup: a public subnet with a public
IP assigned (no NAT Gateway required) and a security group that allows all
egress and **no** inbound rules:

```bash
VPC_ID=$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text)
SUBNET_ID=$(aws ec2 describe-subnets --filters Name=vpc-id,Values=$VPC_ID --query 'Subnets[0].SubnetId' --output text)

SG_ID=$(aws ec2 create-security-group --group-name search-sciops-bot --description "search-sciops-bot: egress only" \
  --vpc-id $VPC_ID --query 'GroupId' --output text)
# default egress-all rule is created automatically; explicitly not adding any ingress rule.
```

If your org requires no public IPs on tasks, use a private subnet with a
NAT Gateway (or VPC endpoints for ECR/Secrets Manager/CloudWatch Logs)
instead — costs more (~$32/mo for a NAT Gateway) but keeps the task off the
public internet entirely. Either way the security group stays inbound-empty.

## 6. Create the service

```bash
aws ecs create-service \
  --cluster search-sciops-bot \
  --service-name search-sciops-bot \
  --task-definition search-sciops-bot \
  --desired-count 1 \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNET_ID],securityGroups=[$SG_ID],assignPublicIp=ENABLED}" \
  --deployment-configuration "minimumHealthyPercent=0,maximumPercent=100,deploymentCircuitBreaker={enable=true,rollback=true}" \
  --region <REGION>
```

`desiredCount=1` is intentional — this is a single stateful Socket Mode
connection, not a horizontally-scaled service. If the task crashes, ECS
replaces it automatically.

The `--deployment-configuration` matters for exactly that reason. ECS
defaults to `minimumHealthyPercent=100, maximumPercent=200`, which at
`desiredCount=1` starts the replacement task **before** draining the old
one. Both would hold Socket Mode connections, and Slack load-balances events
across an app's connections — so during the overlap a thread reply can be
delivered to the task that has no session for that thread (`threads` is
per-process; see the last section). `minimumHealthyPercent=0, maximumPercent=100`
forces stop-then-start instead: a few seconds of downtime during a deploy,
but never two bots at once. `deploymentCircuitBreaker` with `rollback` means a
task that can't stay up — bad image, missing env var — reverts to the last
working task definition instead of leaving the service down.

## 7. Verify

```bash
aws logs tail /ecs/search-sciops-bot --follow --region <REGION>
```

Look for Slack Bolt's own startup log (connection established), then mention
the bot in the channel it was invited to.

## Redeploying after a code change

```bash
docker build -f sciops/Dockerfile -t search-sciops-bot sciops
docker tag search-sciops-bot:latest <ACCOUNT_ID>.dkr.ecr.<REGION>.amazonaws.com/search-sciops-bot:latest
docker push <ACCOUNT_ID>.dkr.ecr.<REGION>.amazonaws.com/search-sciops-bot:latest

aws ecs update-service --cluster search-sciops-bot --service search-sciops-bot \
  --force-new-deployment --region <REGION>
```

If goldie's or tuner's `SKILL.md`/scripts changed: re-run that agent's own
`agent_setup.py`, **then re-run `sciops/agents/orchestrator/agent_setup.py`**
(the coordinator's roster pins specific sub-agent versions at creation time
and won't pick up the new one otherwise) — then copy the new `ORCHESTRATOR_*`
values from `sciops/agents/orchestrator/.env` into the task definition's
`environment` block, register a new revision, and `update-service
--task-definition search-sciops-bot:<NEW_REVISION>`. No image rebuild is
needed for an agent-only change.

## Optional: tune session timeouts

`slack_bot.py` automatically archives a thread's Managed Agent session after
`SESSION_INACTIVITY_TIMEOUT_S` seconds of inactivity (default 1800 = 30 min), checked every
`SESSION_SWEEP_INTERVAL_S` seconds (default 60) — see `README.md`'s "Session lifecycle"
section. It also enforces `MAX_CONCURRENT_THREADS` (default 25) before creating
new sessions, and `SHUTDOWN_GRACE_S` (default 90) bounds how long it spends
archiving sessions on SIGTERM. These are plain (non-secret) env vars; add them
to the task definition's `containerDefinitions[0].environment` array (not
`secrets`) only if the defaults don't fit — no code or IAM change needed
either way.

If you raise `SHUTDOWN_GRACE_S`, also raise the container definition's
`stopTimeout` (currently 120) to stay above it — SIGKILL lands at
`stopTimeout` regardless of whether the handler has finished.

## Graceful shutdown

On SIGTERM (every redeploy and scale-in) `slack_bot.py`'s `_shutdown` posts an
"I'm restarting, so this thread won't carry over" note to each active thread,
then archives each of their sessions, retrying the ones still mid-turn until
`SHUTDOWN_GRACE_S` runs out. `stopTimeout: 120` in the task definition gives it
room to finish before SIGKILL — the ECS default is 30s. Sessions still not
`idle` at the deadline are logged by session id, so `aws logs tail` after a
deploy tells you whether anything needs archiving by hand.

## Known limitation: in-memory thread↔session mapping

`slack_bot.py`'s `threads` dict lives in process memory. A task restart
drops it — replies in existing Slack threads will no longer find their
session, and a fresh `@mention` is needed to start a new one. The graceful
shutdown above covers the *planned* cases (redeploy, scale-in): sessions get
archived and their threads notified before the process exits. It cannot cover
a hard crash, a SIGKILL, or a Fargate Spot interruption — there, the orphaned
sessions stay open on Anthropic's side until someone archives them manually,
since the inactivity sweeper only sees `threads` entries created by the
*current* process. If unplanned restarts are frequent enough for this to
matter, persist thread state externally (e.g. DynamoDB) rather than keeping it
in-process. Regular Fargate (not Spot) avoids the interruption case.
