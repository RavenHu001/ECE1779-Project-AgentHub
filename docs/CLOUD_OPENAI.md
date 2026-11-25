# Cloud OpenAI deployment

Local `.env` files are ignored by Git and excluded from images. The cloud needs a
Swarm secret, non-secret manager configuration, and a deployment. Never commit an
API key or put it in a PR, build argument, service environment spec, or CI output.

## Manager prerequisites

Install this reviewed revision of `deploy/scripts` and `deploy/swarm` under the
existing root-owned `/opt/agenthub/deploy/`. The release workflow sends an image
digest; it does not update these scripts or create credentials. Set the following
in the trusted `/etc/agenthub-delivery/config` for rolling releases, or ignored
`.env.cloud` for staged deployments:

```bash
REPLY_MODE=openai
OPENAI_MODEL=gpt-5.4-mini
OPENAI_API_KEY_SECRET=agenthub-openai-v1
```

On the intended Swarm manager, create that secret once using a hidden prompt and
stdin, with shell tracing disabled:

```bash
read -r -s -p 'OpenAI API key: ' openai_key
printf '\n'
test -n "$openai_key" && printf '%s' "$openai_key" | docker secret create agenthub-openai-v1 -
unset openai_key
```

If the name exists, verify the intended secret rather than replacing it. Rotation
uses a new versioned name; retain the previous secret until recovery is no longer
needed. Only workers receive the key, not API, database, or setup containers.

## Deployment and recovery

Follow [the approved release procedure](CLOUD_DEPLOYMENT.md#approved-rolling-releases-and-rollback),
including its production approval gate. The browser-session migration runs before
API/worker updates. Merging the PR alone does not deploy it.

With `REPLY_MODE=openai`, the staged deployment layers `swarm/openai.yaml` over
`swarm/stack.yaml`. Local checks default to fixed replies and require no key.
Manual parsing for OpenAI uses both files:

```bash
docker stack config -c deploy/swarm/stack.yaml -c deploy/swarm/openai.yaml > /dev/null
```

Rolling releases reconcile provider settings as well as images. Both APIs and
workers receive the requested mode; workers alone receive the model and secret
mount. A startup wrapper reads `/run/secrets/openai_api_key` into the worker
process environment without printing it or storing its value in the Docker
service specification. It also supports older images using `OPENAI_API_KEY`.

If the image is already running, configuration is still applied without rerunning
migration or replacing the existing image rollback record. Image rollback retains
the manager's desired provider settings. For scripted fallback, set
`REPLY_MODE=fixed` before rollout/rollback; the OpenAI mount is removed.

## Verification

Preflight rejects a missing secret before service changes. The final monitor uses
`--reply-mode openai` to reject a mismatched API configuration. These checks do not
prove provider authentication or billing availability.

After deployment, sign in and send one harmless ordinary message. Confirm its job
succeeds and a model response appears; the worker's existing model usage counters
provide additional evidence. This deliberate provider check can incur charges.
Use an eligible synthetic ticket to confirm a proposal still waits for explicit
human approval. The existing fixed-response deployment smoke script cannot prove
an OpenAI response. PR CI uses simulated provider responses and isolated test data.
