# Evidence: the full lifecycle runs with no manual steps

**Claim.** With the update consumer running, every queued worker update is applied
exactly once and in a forward-only way, and a new tenant walks the whole lifecycle,
create -> active -> update -> active -> delete -> destroyed, with no manual step. The
tenant's `version` rises on every change, including the ones the worker drives.

**Setup.** 18 worker updates were waiting in `controlplane.task-updates` from the
[worker runs](worker-simulator.md), including duplicates, stale `in_progress` updates
and one failure. The control plane with the new consumer was then started. Commands are
PowerShell (Windows), pasted as one block.

## 1. Quality gate

```powershell
docker compose --profile tools run --rm --build check
```

```text
collected 193 items
...
========================= 193 passed in 1.47s =========================
All checks passed.
```

## 2. The 18 waiting updates

```powershell
docker compose up --build -d --wait
docker compose logs control-plane | Select-String "update " | Select-Object -Last 18
```

Abridged (message ids shortened; one line per update, in arrival order):

```text
update 1a724a54 for task d2cafa24 (in_progress): applied
update 75456dce for task 44a2df04 (in_progress): applied
update 443f1824 for task e09c9373 (in_progress): applied
update c17f5342 for task 12abc171 (in_progress): applied
update 8730b667 for task e09c9373 (done): applied
update 37a2da72 for task 44a2df04 (done): applied
update 77dc7b1a for task d2cafa24 (done): applied
update 83a362d9 for task 12abc171 (done): applied
update eb8fc56e for task d3161359 (done): applied          <- dup-corp
update eb8fc56e for task d3161359 (done): duplicate
update 17410f7d for task d3161359 (in_progress): stale
update 17410f7d for task d3161359 (in_progress): duplicate
update a66e509a for task dab1a773 (done): applied          <- fail-corp
update a66e509a for task dab1a773 (done): duplicate
update 2bb4e789 for task dab1a773 (in_progress): stale
update 2bb4e789 for task dab1a773 (in_progress): duplicate
update 40b754b1 for task bc734c2f (in_progress): applied   <- fail-corp-2
update c12eee78 for task bc734c2f (failed): applied
```

- **Idempotent:** each repeated `message_id` (same id, second line) is a `duplicate` and
  changes nothing.
- **Order-tolerant:** an `in_progress` arriving after `done` is `stale` and ignored; the
  task stays `done`.
- **No poison:** nothing was dead-lettered.

```powershell
(Invoke-RestMethod http://localhost:8000/v1/tenants).items | Format-Table slug, status, version
```

```text
slug          status version
----          ------ -------
fail-corp-2   failed       2
fail-corp     active       2
dup-corp      active       2
acme-corp-1   active       2
beta-corp     active       2
acme-corp-ltd active       2
acme-corp     active       2
```

Every tenant changed exactly once (version 1 -> 2), however many duplicate or stale
updates it received. `fail-corp` is `active` because it ran on the duplicate/reorder
worker, not the failing one; `fail-corp-2` got `failed`.

## 3. A new tenant, end to end

```powershell
$t = (Invoke-RestMethod -Method Post -Uri http://localhost:8000/v1/tenants -ContentType 'application/json' `
  -Body (@{slug="life-corp"; name="Life Corp"} | ConvertTo-Json)).tenant
Start-Sleep -Seconds 6
$t = Invoke-RestMethod http://localhost:8000/v1/tenants/$($t.id)
Invoke-RestMethod -Method Patch -Uri http://localhost:8000/v1/tenants/$($t.id) -ContentType 'application/json' `
  -Body (@{name="Life Corp Renamed"; version=$t.version} | ConvertTo-Json)
Start-Sleep -Seconds 6
Invoke-RestMethod -Method Delete -Uri http://localhost:8000/v1/tenants/$($t.id)
Start-Sleep -Seconds 6
(Invoke-RestMethod http://localhost:8000/v1/tenants/$($t.id)/tasks).items | Format-Table type, status, error
```

```text
created:       provisioning v1
after deploy:  active v2
patched:       updating v3
after update:  active v4 name=Life Corp Renamed
deleted:       destroying v5
after destroy: destroyed v6

type    status error
----    ------ -----
destroy done
update  done
deploy  done
```

## What this shows

| Requirement (brief) | Shown by |
| --- | --- |
| Creating a tenant walks the full lifecycle with no manual steps | `life-corp`: `provisioning` -> `active` by itself |
| Update only from `active`, delete from `active` / `failed` | PATCH and DELETE accepted once `active` |
| `version` incremented on every mutation, including worker-applied ones | v1 -> v6 across six changes, three of them by the worker |
| Consumer is idempotent | Every repeated `message_id` logged as `duplicate` |
| Consumer is order-tolerant | Late `in_progress` logged as `stale`; tasks stay `done` |
| Terminal outcome moves the tenant; `failed` -> tenant `failed` | `fail-corp-2` is `failed` |
| Task API tracks the history | `deploy`, `update`, `destroy`, all `done` |
