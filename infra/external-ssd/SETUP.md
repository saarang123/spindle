# External SSD — `spindle-ext`

Replaceable storage volume on the control node. Used for things that can disappear without breaking state: model weights, Docker images, build caches, DB backups.

## Drive

| | |
|---|---|
| Mount path | `/Volumes/spindle-ext` |
| Volume name | `spindle-ext` |
| Filesystem | APFS (GPT) |
| Connection | Thunderbolt 4 (PCI-Express tunnel) |
| Hardware | MSI M482 2TB NVMe in Zike (Gopod) TB4 enclosure |
| Capacity | 2.0 TB |
| Sustained write (F_FULLFSYNC, 2 GB) | **1549.6 MB/s** |

Buffered/burst speeds will be ~2.5–3 GB/s in practice. The fsync number is the conservative "committed-to-NAND" floor.

## Layout

```
/Volumes/spindle-ext/
├── models/
│   └── huggingface/      # ~/.cache/huggingface symlinked here
├── docker/               # Docker Desktop disk image (manual move, see below)
├── workspaces/           # large experiment dirs, datasets
├── caches/               # other tool caches (npm, pip, etc.)
└── backups/
    └── mongo/            # nightly mongodump target (TBD)
```

## HuggingFace cache

Migration: **not needed** — `~/.cache/huggingface` did not exist on this machine. Created a fresh symlink:

```
~/.cache/huggingface -> /Volumes/spindle-ext/models/huggingface
```

First downloads will land directly on the external SSD.

## Manual TODO — Docker Desktop disk image

Agents can't drive Docker Desktop's UI. Do this by hand:

1. Quit Docker Desktop.
2. Settings → Resources → Advanced → **Disk image location**.
3. Click **Move** → select `/Volumes/spindle-ext/docker/`.
4. Wait — copy can take 5–30 min depending on existing image size.
5. Restart Docker Desktop. Verify with `docker info | grep "Docker Root Dir"`.

## DO NOT put on this drive

External drives can disconnect. State stores corrupt if their backing storage vanishes mid-write. These stay on **internal NVMe**:

- MongoDB data dir (`/opt/homebrew/var/mongodb`)
- Redis dump (`/opt/homebrew/var/db/redis`)
- ClickHouse data dir (when added)
- The spindle repo itself
- `.env` files
- Anything else that requires guaranteed disk presence

## Disconnection behavior

If `spindle-ext` unmounts (cable bump, sleep glitch, etc.):

- **Affected**: model loads, Docker images, workspaces, build caches → unavailable until reconnect. Re-downloadable, no data loss for replaceable items.
- **Unaffected**: Mongo/Redis/ClickHouse and the repo — they live on internal NVMe.
- **Watch out**: the HuggingFace symlink will dangle. `transformers` will fail to load cached models until the drive reattaches.

## Backups (TBD)

The right use of `backups/mongo/` is a nightly `mongodump` via `cron` or `launchd`. Not yet wired up — set up later. Keep ≥7 days of dumps; rotate older ones.
