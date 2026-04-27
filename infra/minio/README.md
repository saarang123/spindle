# MinIO — Spindle's artifact backend

[MinIO](https://min.io/) is an S3-compatible object store you self-host. Spindle's `ArtifactStore` protocol has an `s3` backend that talks to MinIO (and to AWS S3, Cloudflare R2, etc. — same wire protocol).

## What this directory has

```
compose.yaml     docker compose service definition
bootstrap.sh     idempotent setup: data dir, credentials, container start
.env             generated on first run; holds creds (gitignored)
```

## Running it

### Mac mini (dev)

You need Docker Desktop installed. Then:

```bash
cd infra/minio
./bootstrap.sh
```

That's it. Defaults:
- Data lives in `~/spindle/minio-data`
- API at `http://localhost:9000`
- Console at `http://localhost:9001`
- Credentials randomly generated, saved to `infra/minio/.env`

To override:
```bash
MINIO_DATA_DIR=/some/other/path ./bootstrap.sh
```

### DGX Spark (prod)

You need Docker installed:
```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER"  # then re-login so you don't need sudo
```

Then clone the repo and run:
```bash
cd ~/Documents
git clone git@github.com:saarang123/spindle.git    # if not already cloned
cd spindle/infra/minio
MINIO_DATA_DIR=/mnt/spindle-artifacts ./bootstrap.sh
```

(Pick whatever path on the 4TB SSD; that's what you want backing the bytes.)

The script auto-restarts the container on host reboot (`restart: unless-stopped` in compose). No systemd unit needed.

To expose to other nodes on the LAN, the container already binds to `0.0.0.0:9000` and `0.0.0.0:9001`. Just make sure the firewall allows it:

```bash
# Ubuntu / ufw
sudo ufw allow from 192.168.1.0/24 to any port 9000
sudo ufw allow from 192.168.1.0/24 to any port 9001
```

(Adjust the subnet to your LAN.)

## Verifying it works

```bash
curl http://localhost:9000/minio/health/live    # → 200
```

Open the console at `http://<host>:9001` in a browser, log in with the credentials from `.env`, and you can browse buckets.

## Wiring Spindle to it

Once Spindle's S3 artifact backend lands (next iteration), set in your root `.env`:

```bash
SPINDLE_ARTIFACT_BACKEND=s3
SPINDLE_S3_ENDPOINT=http://localhost:9000           # or http://spark.local:9000 from another node
SPINDLE_S3_BUCKET=spindle-artifacts
SPINDLE_S3_ACCESS_KEY=...                            # from infra/minio/.env (MINIO_ROOT_USER)
SPINDLE_S3_SECRET_KEY=...                            # from infra/minio/.env (MINIO_ROOT_PASSWORD)
SPINDLE_S3_REGION=us-east-1                          # arbitrary; MinIO ignores
```

The bucket gets auto-created by the application on first write — no manual `mc mb` step required.

## Stopping / removing

```bash
cd infra/minio
docker compose down                 # stop, keep data
docker compose down -v              # stop, also delete the volume metadata
rm -rf "$HOME/spindle/minio-data"   # delete actual bytes (DESTRUCTIVE)
```

## Migrating to AWS S3 / Cloudflare R2 later

The whole point of using MinIO is that the migration is a config change, not a code change. When you outgrow self-hosted:

1. Provision a bucket on S3 / R2.
2. Update `SPINDLE_S3_ENDPOINT`, `SPINDLE_S3_ACCESS_KEY`, `SPINDLE_S3_SECRET_KEY` to point there.
3. (Optional) Use `mc mirror` to copy existing artifacts from MinIO to the new bucket.

No code changes in `core/`, `api/`, `dispatcher/`, or `workers/`. That's the dividend of putting it behind the `ArtifactStore` protocol.

## Why MinIO instead of just a directory + tiny HTTP server

- Real auth (per-bucket policies, signed URLs that expire).
- Multipart upload for large videos (chunked, resumable).
- Range requests / partial GETs (clients only fetch what they need).
- Lifecycle rules (auto-delete artifacts older than N days).
- Web console for inspection.
- Identical SDK calls for AWS S3 / R2 / B2 the day you migrate.
- One service vs. one-per-node read endpoints.

The cost: one container to keep running. Negligible at this scale.
