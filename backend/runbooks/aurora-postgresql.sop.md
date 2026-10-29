# Aurora PostgreSQL Engine Guide

## Overview
Engine-specific guidelines for Amazon Aurora PostgreSQL. Aurora's architecture differs
significantly from standard PostgreSQL — these rules prevent incorrect recommendations.

## Architecture Differences

### Storage Layer
- Aurora uses a distributed, 6-copy, 3-AZ storage layer
- Storage is shared between writer and reader instances
- No traditional data file management — Aurora handles this at the storage layer
- Storage auto-scales — FreeStorageSpace metric is NOT applicable

### WAL and Checkpoints
- Aurora streams WAL records continuously to its storage layer
- There are NO traditional checkpoint flush storms like in vanilla PostgreSQL
- checkpoint_timeout exists as a parameter but does NOT cause the same I/O spikes
- max_wal_size is NOT applicable — Aurora manages WAL at the storage layer
- MUST NOT blame checkpoint_timeout for CPU spikes on Aurora
- MUST NOT recommend changing max_wal_size on Aurora

### Replication
- Aurora uses storage-level replication (not streaming replication)
- Reader instances share the same storage volume as the writer
- Replication lag is typically <100ms (storage-level, not WAL-level)
- synchronous_commit behavior differs — Aurora's storage provides durability
  at the infrastructure level. However, DO NOT recommend setting synchronous_commit=off
  as a general optimization. Aurora's default (on) ensures application-level consistency.
  Only recommend changing it if the user explicitly asks about commit latency optimization
  AND understands the trade-off (last few uncommitted transactions may be lost on crash).
  Never recommend it as a fix for high CPU or high IOPS — address the workload instead.

## Parameter Management
- ALTER SYSTEM does NOT work on Aurora
- All parameter changes MUST use DB cluster parameter groups or DB instance parameter groups
- Changes are applied via AWS Console, CLI, or CloudFormation
- MUST NOT write ALTER SYSTEM in any recommendation for Aurora
- Instead, recommend: "Modify the cluster parameter group to set <param> = <value>"

## Common False Positives
- aurora_stat_plans() queries — these are Aurora's internal monitoring, NOT production issues
- High temp file usage from aurora_stat_plans() — monitoring overhead, not workload issue
- checkpoint_timeout = 60s — not causing I/O storms on Aurora (unlike vanilla PostgreSQL)

## Performance Insights
- Aurora supports Performance Insights (PI) in standard and advanced modes
- PI uses DbiResourceId (db-XXXX format), not the instance identifier
- PI data is per-instance, not per-cluster — use the writer instance for write workload analysis
- PI requires the pi:GetResourceMetrics permission in the cross-account role
- MUST NOT recommend "Enable Performance Insights" without first checking if it's already
  enabled. If PI data was returned by the tools, PI is already enabled. Only recommend
  enabling PI if the tools returned an error saying PI is not available.

## Recommended Parameter Ranges (Aurora PostgreSQL)
- effective_io_concurrency: 200-256 (SSD-backed storage)
- random_page_cost: 1.0-1.1 (SSD-backed storage)
- work_mem: 4MB-64MB depending on workload (start conservative, increase if temp files observed)
- shared_buffers: Aurora manages this — typically 75% of instance memory
- maintenance_work_mem: 256MB-2GB depending on instance size
