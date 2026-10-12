# Run Husk with Docker

Run Husk's backend and Studio from one container. A single `docker run` gives you the trace endpoint and the dashboard on your own machine. Local-first holds: state stays in a volume you control, the container binds to loopback only, and the only outbound call is the automatic debugger going to the provider you configure.

Husk in a container is the same thing as Husk on your machine — a development-time, single-user debugger you run while building an agent, before production. The container does not change that: ingest is loopback-only, so only an agent running on this host can stream traces in. There is no way to point it at a production deployment.

The image is published to the GitHub Container Registry on every release.

```bash
docker pull ghcr.io/husk-ai-lab/husk-ai
```

## docker run

One command brings up the backend and the Studio on port 7654, bound to loopback:

```bash
docker run -d --name husk -p 127.0.0.1:7654:7654 -v husk-data:/data ghcr.io/husk-ai-lab/husk-ai
```

Open **http://localhost:7654**. Then seed a sample run to look at right away:

```bash
docker exec husk husk-studio demo
```

To see the **node graph + per-node state diff** (the `demo` trace is flat, not a graph), run the bundled example agent on the container's loopback:

```bash
docker exec husk sh -c \
  'OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:7654 husk-studio run python examples/husk_thread.py'
```

## Connecting your own agent

Wiring an agent to Husk is one line: point its OpenTelemetry exporter at the container's loopback endpoint. Because ingest is loopback-only, the agent has to run on this host — typically inside the same container:

```bash
docker exec husk sh -c \
  'OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:7654 husk-studio run python your_agent.py'
```

That one-line OpenTelemetry setup is the single touchpoint between your agent and Husk; everything else stays the same code you already have.

## Ports

The container listens on **7654**. Map it to loopback on the host so nothing outside your machine can reach it:

```bash
-p 127.0.0.1:7654:7654
```

Change the host port by remapping the left side, the container always listens on 7654:

```bash
docker run -d --name husk -p 127.0.0.1:8080:7654 -v husk-data:/data ghcr.io/husk-ai-lab/husk-ai
```

> **Loopback only.** Husk's state-changing routes (trace ingest, replay, debugger) reject non-loopback callers. That is enforced in the backend, not just in this command — binding the container to `127.0.0.1` keeps the rest of the network out as well. Husk cannot be pointed at production by design.

## The /data volume

Everything Husk stores lives at `/data` inside the container (`HUSK_HOME=/data`): the SQLite trace DB (runs, spans, prompts, completions, tool I/O), recorded cassettes, and the debugger key. Mount a named volume there so it survives container restarts and image upgrades:

```bash
-v husk-data:/data
```

Prefer a host path? Swap in a bind mount:

```bash
docker run -d --name husk -p 127.0.0.1:7654:7654 -v /srv/husk:/data ghcr.io/husk-ai-lab/husk-ai
```

Back up by copying that volume or path. Wipe everything by removing it.

## Zero data retention, by default

Nothing leaves your machine on its own. The backend, the Studio, and your traces all stay in the container and its volume. The only outbound call comes from the automatic debugger, and it goes to the provider you configure: **Regolo.ai by default (EU, GDPR, zero-retention)**, or any provider you set in **Settings**. No keys, no AI calls, no outbound traffic until you ask Husk to debug a run.

## Related

- [README](../README.md): what Husk is and the local (non-Docker) quickstart.
- [`docker-compose.yml`](../docker-compose.yml): the same single-container, loopback-bound setup as a Compose file (`docker compose up -d`).
