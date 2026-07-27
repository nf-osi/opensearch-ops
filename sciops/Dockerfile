# search-sciops-bot: Slack bot for the search-relevance orchestrator Managed
# Agent (slacker/ within this sciops/ build context). The runtime config now
# comes from real env vars (or a local slacker/.env during dev), so the image
# no longer depends on an external orchestrator .env path.
#
# Nothing else: goldie, tuner, and the orchestrator are all self-contained —
# tuner gets each table's golden.yaml/fields.yaml from a source named in the
# request or from wherever they're already mounted, and is deliberately
# repo-agnostic with no default repo (see sciops/agents/tuner/SKILL.md), so
# nothing depends on this image's own checkout.
#
# Deployment runbook: sciops/slacker/deploy/DEPLOY.md
FROM python:3.12-slim

WORKDIR /app

COPY slacker/requirements.txt slacker/requirements.txt
RUN pip install --no-cache-dir -r slacker/requirements.txt

COPY . sciops/

WORKDIR /app/sciops/slacker
CMD ["python3", "slack_bot.py"]
