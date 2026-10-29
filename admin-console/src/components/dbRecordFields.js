// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
// The fields the admin console reads off a database record from GET /api/databases.
// A space admin gets the MANAGER projection (backend/utils/db_projection.py,
// project_db_for_manager), an allowlist: a field it does not return renders as an
// empty cell rather than an error. backend/tests/test_console_db_fields.py collects
// every `db.<field>` the console reads (SpaceDetail.jsx) and fails when one is neither
// listed here and returned by the projection, nor a candidate-only field below.

export const DB_RECORD_FIELDS = [
  'instance_id', 'display_name', 'status', 'account_id', 'engine', 'region', 'endpoint',
  'port', 'database_name', 'secret_arn', 'member_space_ids', 'member_count',
  'cluster_member_roles', 'config_error', 'config_log', 'tags',
  'tier1_enabled', 'tier1_frequency', 'tier2_enabled', 'tier2_frequency',
  'route_repair_needed', 'route_conflict',
]

// Read from `db.tags`.
export const DB_RECORD_TAGS = ['raw_identifier', 'replica_of']

// Carried by a GET /api/spaces/{id}/candidates row (list_space_candidates), never by a
// database record: the onboarding wizard reads them when it opens on a candidate.
// (A record carries a replica's source as tags.replica_of instead.)
export const CANDIDATE_ONLY_FIELDS = [
  'onboarded_elsewhere', 'shared_with_count', 'suggested_secret_arn', 'suggested_secret_from',
  'replica_of',
]
