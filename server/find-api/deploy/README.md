# Find My Drone API — go-live (needs Callin's OK)

Nothing in this folder has been applied to the VPS. The service, systemd
unit, and Caddy change must wait for an explicit OK.

## What was checked (read-only) on the VPS

- 6 CPUs (Intel Haswell KVM), 11 GiB RAM, no swap
- About 9.2 GiB MemAvailable at the check, load 0.17
- `/dev/sda1` 96 G, 25 G free (75% used)

Caps baked into the unit: 2 OpenCV threads, 1 match at a time, 250 MiB
upload, 2 GiB store, 4 mosaics, 12 h TTL, 500 Mpixel decode cap, 8000 px
working dim, MemoryMax=3G, CPUQuota=200%, bind 127.0.0.1 only.

## After OK, on the FPV laptop (PowerShell)

Do not put `$` in a `plink` command string (PowerShell will expand it).
Copy the tree, then run the install script *on the server*:

```
pscp -batch -i C:\Users\calli\.ssh\keys\server-key-putty.ppk -r find-api ubuntu@40.160.141.85:drone-mapper-find-src
plink -batch -i C:\Users\calli\.ssh\keys\server-key-putty.ppk ubuntu@40.160.141.85 "sudo bash drone-mapper-find-src/deploy/deploy.sh --yes"
```

The script installs to `/opt/drone-mapper-find`, data in
`/var/lib/drone-mapper-find`, and starts `drone-mapper-find.service` on
`127.0.0.1:8091`. It does **not** edit the Caddyfile.

## Caddy (after the service is healthy)

1. Backup `/etc/caddy/Caddyfile`.
2. Insert `deploy/caddy-snippet.caddy` inside the `wheresthepelican.com`
   site block, **before** the existing `handle_path /drone-mapper/*`
   static rule, so `/drone-mapper/api/*` is not swallowed by the
   static site.
3. `caddy validate --config /etc/caddy/Caddyfile` then reload Caddy.
4. Confirm `https://wheresthepelican.com/drone-mapper/api/health` returns
   `{"ok": true, ...}` and that `/`, `/drone-mapper/`, the APK, Quantum
   Stockpile, and WebODM routes are unchanged.

Then deploy the updated static planner (Find My Drone tab) to
`/var/www/drone-mapper`. The live page was held back so the tab would
not call a missing API.

## What this does not touch

Quantum Stockpile, WebODM, Kalshi, paper-feed, the hosted APK, and any
existing Caddy routes other than the new `/drone-mapper/api/*` handle.
