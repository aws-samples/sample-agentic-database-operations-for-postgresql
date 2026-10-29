# RDS PostgreSQL Engine Guide

## Overview
Engine-specific guidelines for Amazon RDS PostgreSQL (non-Aurora). Standard PostgreSQL
running on RDS with managed infrastructure.

## Architecture
- Standard PostgreSQL with EBS-backed storage
- Traditional checkpoint behavior — checkpoint_timeout and max_wal_size matter
- Standard streaming replication for read replicas
- Storage auto-scales ONLY when RDS storage autoscaling is enabled (MaxAllocatedStorage
  set) — it then grows automatically up to that cap. If autoscaling is off, storage does
  not grow on its own. Check MaxAllocatedStorage before asserting either way, and monitor
  FreeStorageSpace

## Parameter Management
- ALTER SYSTEM is supported for some parameters but parameter groups are recommended
- Changes via parameter groups are persistent across reboots
- ALTER SYSTEM changes may be overridden by parameter group on reboot
- Recommend: "Modify the DB parameter group to set <param> = <value>"

## Checkpoint Tuning (applies to RDS PostgreSQL, NOT Aurora)
- checkpoint_timeout: default 5min, increase to 10-15min for write-heavy workloads
- max_wal_size: default 1GB, increase to 2-4GB to reduce checkpoint frequency
- checkpoint_completion_target: 0.9 (default, usually fine)
- High checkpoint frequency CAN cause I/O spikes on RDS PostgreSQL

## Recommended Parameter Ranges (RDS PostgreSQL)
- effective_io_concurrency: 200 for gp3/io1 SSD, 1-2 for magnetic
- random_page_cost: 1.1 for SSD, 4.0 for magnetic
- work_mem: 4MB-64MB depending on workload
- shared_buffers: 25% of instance memory (RDS default)
- maintenance_work_mem: 256MB-2GB depending on instance size
- max_wal_size: 2GB-4GB for write-heavy workloads
