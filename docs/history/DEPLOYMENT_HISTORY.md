# Deployment history

## Domain: why bibchatbot.com, not chatbot.hmwagner.com

The original plan was to deploy under `chatbot.hmwagner.com`. That domain was
never reachable because its authoritative nameservers turned out to be
Network Solutions/WorldNIC, not Google Cloud DNS, and no one had working
registrar access to redelegate them. `bibchatbot.com` was registered
specifically to have DNS the team actually controls, and is what production
runs under now (see `docs/PRODUCTION_READINESS.md`, deployment-topology gate).

## Cloud Run → single-host migration

The service originally ran on Cloud Run (`tma-backend-873813047759.us-east4.run.app`).
That URL now 404s (a stale pre-`/readyz` build) and should be decommissioned;
production moved to the single persistent Docker host described in
`docs/DEPLOYMENT_SINGLE_HOST.md`.
