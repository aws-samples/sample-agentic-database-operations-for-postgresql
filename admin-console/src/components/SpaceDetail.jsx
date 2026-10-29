// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React, { useState, useEffect } from 'react'
import { APP_NAME } from '../appName'
import WebAppUsers from './WebAppUsers'
import AgentCapabilities from './AgentCapabilities'
import { config } from '../config'
import { AlertTriangle, ArrowLeft, ArrowRight, Check, Copy, ExternalLink, Home, Lock, RefreshCw, ScrollText, Search, Settings2, Trash2, Zap } from 'lucide-react'
import TopologyMap from './TopologyMap'
import SideSheet, { StepIndicator } from './SideSheet'
import AuditLog from './AuditLog'
import { memberCountLabel } from './MemberPicker'
import { useDbListControls, DbListToolbar, DbListPager, pageNested, engineLabel } from './DbListControls'

const WEBAPP_BASE_URL = config.webAppUrl

const AWS_REGIONS = [
  'us-east-1', 'us-east-2', 'us-west-1', 'us-west-2',
  'eu-west-1', 'eu-west-2', 'eu-west-3', 'eu-central-1', 'eu-north-1',
  'ap-southeast-1', 'ap-southeast-2', 'ap-northeast-1', 'ap-northeast-2', 'ap-south-1',
  'sa-east-1', 'ca-central-1',
]

// Region chips are grouped by geography so the picker stays scannable as the list grows.
const REGION_GROUPS = [
  { label: 'Americas', test: r => /^(us|ca|sa|mx)-/.test(r) },
  { label: 'Europe', test: r => /^eu-/.test(r) },
  { label: 'Asia Pacific', test: r => /^ap-/.test(r) },
  { label: 'Middle East & Africa', test: r => /^(me|af|il)-/.test(r) },
]
const STATUS_BADGE = {
  discovered: { label: 'Discovered', cls: 'bg-warn-bg text-warn-text border-warn-border' },
  configuring: { label: 'Configuring…', cls: 'bg-info-bg text-info-text border-info-border animate-pulse' },
  configured: { label: 'Ready', cls: 'bg-ok-bg text-ok-text border-ok-border' },
  config_failed: { label: 'Failed', cls: 'bg-err-bg text-err-text border-err-border' },
  // Removal runs in the background after the request; the row stays until it is gone.
  deleting: { label: 'Deleting…', cls: 'bg-info-bg text-info-text border-info-border animate-pulse' },
  delete_failed: { label: 'Delete failed', cls: 'bg-err-bg text-err-text border-err-border' },
}
const REMOVING = new Set(['deleting', 'delete_failed'])
// A removal whose server process stopped part way (a deploy or restart) keeps its
// "deleting" claim with no heartbeat; the backend flags it teardown_stalled. Show it as
// Delete failed so the row offers Retry, which takes the claim over and finishes it.
const STALLED_REMOVAL = 'The removal stopped before it finished (the service restarted). Retry finishes it.'
// An onboard whose server process stopped part way (a deploy or restart) stays at
// "configuring" with no worker to finish it; the backend flags it configure_stalled. Show
// it as Failed so the row offers Onboard/Retry and the spinner clears. Mirrors the
// deleting+teardown_stalled branch below (the backend reclassify is primary; this is the
// symmetric client-side net).
const STALLED_CONFIGURE = 'Configuration did not finish (the service restarted). Onboard it again.'
const showStalledAsFailed = d => {
  if (d && d.status === 'deleting' && d.teardown_stalled)
    return { ...d, status: 'delete_failed', config_error: d.config_error || STALLED_REMOVAL }
  if (d && d.status === 'configuring' && d.configure_stalled)
    return { ...d, status: 'config_failed', config_error: d.config_error || STALLED_CONFIGURE }
  return d
}

// What an onboard is doing, shown inline under the row. While a database is still
// `configuring` this streams the live setup log — the latest step plus the full
// step-by-step log in a collapsible block, refreshed by the 3s poll — so the progress that
// used to scroll by in the foreground onboard is visible again now that onboarding runs in
// the background. When it ends in `config_failed` the same block turns red and leads with
// the one-line reason. A database explains itself here instead of leaving the state in a
// hover tooltip (or only in the record no one can see).
function FailureDetail({ db, colSpan, onClaimRoute }) {
  const failed = db.status === 'config_failed'
  // Offered only when the server recorded an unresolved conflict on the agent's OWN CIDR
  // route (claimable); never for any other route. The handler confirms the consequence.
  const claimable = failed && !!(db.route_conflict && db.route_conflict.claimable) && !!onClaimRoute
  const configuring = db.status === 'configuring'
  if (!failed && !configuring) return null
  const log = Array.isArray(db.config_log) ? db.config_log : []
  // An IAM denial carries a long base64 "Encoded authorization failure message" that an
  // operator cannot decode and that, as one unbroken token, widens the table and pushes the
  // row's actions off screen. Drop it from the shown reason; the useful part is before it.
  const reason = (db.config_error || '').split(/\s*Encoded authorization failure message:/i)[0].trim()
  const latest = log.length > 0 ? log[log.length - 1] : ''
  // A configuring row with nothing streamed yet stays quiet — the "Configuring…" badge
  // already carries the state, so an empty bar would only add noise.
  if (configuring && !latest) return null
  if (failed && !reason && log.length === 0) return null
  const tone = failed ? 'bg-err-bg text-err-text' : 'bg-info-bg text-info-text'
  return (
    <tr>
      {/* break-words wraps the long unbroken tokens so the text can never widen the table;
          the reason/step is height-capped and the full log stays collapsed, so the row
          cannot grow tall enough to hide the rows (and their actions) below it. */}
      <td colSpan={colSpan} className={`px-3 py-2 ${tone} text-[11px] break-words`}>
        {failed && reason && (
          <div className="max-h-24 overflow-y-auto">
            <span className="font-medium">Configuration failed:</span> {reason}
          </div>
        )}
        {claimable && (
          <div className="mt-1.5 flex items-center gap-2">
            <button type="button" onClick={() => onClaimRoute(db)}
              className="px-2.5 py-1 text-[11px] font-medium rounded-md border border-err-border bg-surface text-err-text hover:bg-err-bg-2">
              Claim route
            </button>
            <span className="text-[10px] opacity-80">
              Repoints the database VPC's route for the agent CIDR to this onboarding's peering. Only if you are sure the current holder is unused.
            </span>
          </div>
        )}
        {configuring && (
          <div className="flex items-center gap-1.5 max-h-24 overflow-y-auto">
            <RefreshCw className="w-3 h-3 animate-spin flex-shrink-0" aria-hidden />
            <span><span className="font-medium">Configuring:</span> {latest}</span>
          </div>
        )}
        {log.length > 0 && (
          <details className="mt-1">
            <summary className="text-[10px] cursor-pointer hover:underline">Show setup log ({log.length} steps)</summary>
            <pre className="mt-1 bg-surface-2 rounded p-2 text-[10px] text-fg-3 font-mono overflow-auto max-h-48 break-words whitespace-pre-wrap">{log.join('\n')}</pre>
          </details>
        )}
      </td>
    </tr>
  )
}
// 1 min and 2 min are not offered: a cluster's Tier 1 run can take up to 3 minutes, so a
// shorter cadence overlaps runs. The server resolves any legacy stored value to 5 min.
const TIER1_OPTIONS = [
  { value: '5min', label: '5 min' }, { value: '10min', label: '10 min' },
  { value: '15min', label: '15 min' }, { value: '30min', label: '30 min' },
]
const TIER2_OPTIONS = [
  { value: '1h', label: '1 hour' }, { value: '3h', label: '3 hours' },
  { value: '6h', label: '6 hours' }, { value: '12h', label: '12 hours' },
  { value: 'daily', label: 'Daily' }, { value: 'weekly', label: 'Weekly' },
]

// Credentials and access roles are keyed on the admin who OWNS a space (`owner_admin`,
// the creator), not on the space: all of one admin's spaces share one role per account
// and one Secrets Manager namespace, so a database onboarded in one is added to the
// others with the same secret. Mirrors tools/resource_names.py; tests pin the two.
const ownerAdminOf = (space) => space?.owner_admin || space?.created_by || ''
export const ownerKey = (ownerAdmin) => (ownerAdmin || '').replace(/-/g, '').slice(0, 12).toLowerCase() || 'unknown'
function suggestedRoleName(space) { return `${config.resourcePrefix}-agent-${ownerKey(ownerAdminOf(space))}` }
const SUGGESTED_POLICY_NAME = `${config.resourcePrefix}-agent-policy`

// The policy an operator pastes onto the role they create in a secondary account.
// Defined once and used by both the copy button and the block on screen - they were
// two inline copies of the same 4 KB literal, free to drift apart.
//
// Read-only credentials are scoped to this deployment's own secret namespace, matching
// the policy /api/agent-role-policy returns. Scoping secretsmanager to that namespace
// rather than '*' keeps the role an operator creates from these instructions from reading
// every secret in their account, and in step with the permissions boundary this
// deployment applies to the roles it creates itself.
//
// The remaining '*' resources are actions with no resource-level form (describe/list
// across ec2, vpc-lattice, ram, cloudwatch, pi, logs) or targets that are not
// knowable until a peering is proposed.
//
// ReadOwnRoleTags lets the role read the tags of `<prefix>-agent-*` roles, so the account
// check can read its own DBOpsPolicyVersion tag (MEMBER_POLICY_VERSION) as the role.
//
// Network mutations are constrained by the ManagedBy tag: creates must request it,
// deletes and modifies require the resource to already carry it, and ec2:CreateTags is
// limited to tag-on-create so the tag cannot be applied to a pre-existing resource to
// unlock those statements. Three groups stay unconditioned because they act on resources
// the product does not own and cannot tag — the customer's route tables, the database's
// own security groups, and the accepter side of a peering created in the agent account.
// The network-interface actions VPC Lattice needs for a resource gateway are allowed only
// when an AWS service makes the call (aws:ViaAWSService): tested in a dev account, the
// gateway's and the endpoint's interfaces are created by the AWSServiceRoleForVpcLattice
// service-linked role, and the same role calling ModifyNetworkInterfaceAttribute or
// DeleteNetworkInterface directly is denied.
//
// This block is the single rendering of the member-account role's permission policy: an
// operator pastes it onto a role they create by hand. tests/test_secondary_role_policy.py
// pins its least-privilege properties (every mutating statement is conditioned on the
// ManagedBy tag, ec2:CreateTags is tag-on-create only, and no statement grants a service
// wildcard), and fails the build if one loosens.
// GET /api/agent-role-policy is a DIFFERENT, narrower policy — the per-space access role
// the app provisions itself, which has no network permissions at all.
// Trust policy for the member-account role. Kept as one literal: the cross-account
// tests read it from this file to check the owner pin (sts:ExternalId = the admin who
// owns the space) and the roles-only principal narrowing (aws:PrincipalArn).
function memberRoleTrustPolicy(space) {
  return {"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"AWS":`arn:aws:iam::${space.primary_account_id}:root`},"Action":"sts:AssumeRole","Condition":{"StringEquals":{"sts:ExternalId":ownerAdminOf(space)},"ArnLike":{"aws:PrincipalArn":`arn:aws:iam::${space.primary_account_id}:role/*`}}}]}
}

// An RDS read replica is its own database, listed directly under its primary. `replicaOf`
// is the primary's RDS identifier as RDS reports it (recorded by discovery), matched against
// each row's raw identifier or display name. A replica whose primary is not in the list
// keeps its place and still shows "replica of <name>". Returns [{ row, depth, parentName }].
function nestReplicas(rows, replicaOf) {
  const keyOf = r => [r.raw_identifier || r.tags?.raw_identifier, r.display_name].filter(Boolean)
  const byKey = new Map()
  rows.forEach(r => keyOf(r).forEach(k => byKey.set(k, r)))
  const children = new Map()
  const roots = []
  rows.forEach(r => {
    const parent = replicaOf(r) ? byKey.get(replicaOf(r)) : null
    if (parent && parent !== r) children.set(parent, [...(children.get(parent) || []), r])
    else roots.push(r)
  })
  const out = []
  const seen = new Set()
  const emit = (r, depth) => {
    if (seen.has(r)) return
    seen.add(r)
    out.push({ row: r, depth, parentName: replicaOf(r) })
    ;(children.get(r) || []).forEach(c => emit(c, depth + 1))
  }
  roots.forEach(r => emit(r, 0))
  rows.forEach(r => emit(r, 0))   // anything left (a replica cycle) keeps a place
  return out
}

// The "replica of X" sub-label shown under a replica's name.
function ReplicaLabel({ name }) {
  if (!name) return null
  return <span className="block text-[10px] font-normal text-fg-muted">replica of {name}</span>
}

// Single-quote a JSON document for a bash/zsh argument.
const shellQuote = (text) => `'${text.replace(/'/g, `'\\''`)}'`

// The two CLI calls that create the member-account role with exactly the trust and
// permission policies the manual walkthrough shows. Everything is prefilled from the
// space; the operator only chooses which account's credentials to run it under.
function memberRoleCliCommand(space, roleName, accountId) {
  const name = (roleName || '').trim() || suggestedRoleName(space)
  return [
    'aws iam create-role \\',
    `  --role-name ${name} \\`,
    `  --description "Cross-account read access for ${APP_NAME} (admin ${ownerKey(ownerAdminOf(space))}, all their spaces)" \\`,
    `  --assume-role-policy-document ${shellQuote(JSON.stringify(memberRoleTrustPolicy(space)))} \\`,
    `  --tags Key=ManagedBy,Value=${config.resourcePrefix}-agent`,
    '',
    memberRolePolicyUpdateCommand(space, name, accountId),
  ].join('\n')
}

// Version of secondaryAccountPolicy below. The commands tag the role with it once the
// policy is applied, and the account check (POST /api/spaces/{id}/accounts/check) reads
// the tag to tell a current role from an outdated one. Mirrors MEMBER_POLICY_VERSION in
// backend/app.py. Bump it whenever the policy changes: tests/test_secondary_role_policy.py
// fails if the policy changes and this does not.
const MEMBER_POLICY_VERSION = '2'
const POLICY_VERSION_TAG = 'DBOpsPolicyVersion'

// Applies the current permission policy to an existing role, then records its version.
// The tag is written only if the policy was applied (&&).
function memberRolePolicyUpdateCommand(space, roleName, accountId) {
  const name = (roleName || '').trim() || suggestedRoleName(space)
  return [
    'aws iam put-role-policy \\',
    `  --role-name ${name} \\`,
    `  --policy-name ${SUGGESTED_POLICY_NAME} \\`,
    `  --policy-document ${shellQuote(JSON.stringify(secondaryAccountPolicy(config.resourcePrefix, accountId, ownerAdminOf(space))))} \\`,
    `&& aws iam tag-role --role-name ${name} --tags Key=${POLICY_VERSION_TAG},Value=${MEMBER_POLICY_VERSION}`,
  ].join('\n')
}

// Puts this space's trust policy back on an existing role (the fix when its trust no
// longer accepts the deployment).
function memberRoleTrustUpdateCommand(space, roleName) {
  const name = (roleName || '').trim() || suggestedRoleName(space)
  return [
    'aws iam update-assume-role-policy \\',
    `  --role-name ${name} \\`,
    `  --policy-document ${shellQuote(JSON.stringify(memberRoleTrustPolicy(space)))}`,
  ].join('\n')
}

// `accountId` pins the secret read to the member account the role lives in, and
// `ownerAdmin` to the owning admin's secret namespace `<prefix>/<ownerAdmin>/`: the role
// proves access to databases in ITS account with secrets its admin owns, and can read
// nothing else. Another admin's secret, however it is named, is outside this path.
function secondaryAccountPolicy(prefix, accountId = '*', ownerAdmin = '*') {
  return {
    "Version": "2012-10-17",
    "Statement": [
      {
        "Sid": "RDSDiscovery",
        "Effect": "Allow",
        "Action": [
          "rds:DescribeDBClusters",
          "rds:DescribeDBInstances",
          "rds:DescribeDBSubnetGroups",
          "rds:DescribeGlobalClusters",
          "rds:ListTagsForResource"
        ],
        "Resource": "*"
      },
      {
        "Sid": "SecretsAccessScoped",
        "Effect": "Allow",
        "Action": [
          "secretsmanager:GetSecretValue",
          "secretsmanager:DescribeSecret"
        ],
        "Resource": `arn:aws:secretsmanager:*:${accountId || '*'}:secret:${prefix}/${ownerAdmin || '*'}/*`
      },
      {
        "Sid": "CloudWatchMetrics",
        "Effect": "Allow",
        "Action": [
          "cloudwatch:GetMetricStatistics",
          "cloudwatch:GetMetricData",
          "cloudwatch:ListMetrics",
          "cloudwatch:DescribeAlarms",
          "cloudwatch:DescribeAlarmsForMetric"
        ],
        "Resource": "*"
      },
      {
        "Sid": "PerformanceInsights",
        "Effect": "Allow",
        "Action": [
          "pi:GetResourceMetrics",
          "pi:DescribeDimensionKeys",
          "pi:GetDimensionKeyDetails",
          "pi:ListAvailableResourceMetrics",
          "pi:ListAvailableResourceDimensions"
        ],
        "Resource": "*"
      },
      {
        "Sid": "CloudTrail",
        "Effect": "Allow",
        "Action": [
          "cloudtrail:LookupEvents"
        ],
        "Resource": "*"
      },
      {
        "Sid": "CloudWatchLogs",
        "Effect": "Allow",
        "Action": [
          "logs:FilterLogEvents",
          "logs:GetLogEvents",
          "logs:DescribeLogGroups",
          "logs:DescribeLogStreams",
          "logs:StartQuery",
          "logs:GetQueryResults"
        ],
        "Resource": "*"
      },
      {
        "Sid": "NetworkDiscovery",
        "Effect": "Allow",
        "Action": [
          "ec2:DescribeVpcPeeringConnections",
          "ec2:DescribeRouteTables",
          "ec2:DescribeVpcs",
          "ec2:DescribeSubnets",
          "ec2:DescribeSecurityGroups",
          "ec2:DescribeNetworkInterfaces",
          "vpc-lattice:ListResourceGateways",
          "vpc-lattice:GetResourceGateway",
          "vpc-lattice:ListResourceConfigurations",
          "vpc-lattice:GetResourceConfiguration",
          "vpc-lattice:ListTagsForResource",
          "ram:GetResourceShares",
          "ram:GetResourceShareAssociations"
        ],
        "Resource": "*"
      },
      {
        "Sid": "NetworkRouteWrites",
        "Effect": "Allow",
        "Action": [
          "ec2:CreateRoute",
          "ec2:DeleteRoute",
          "ec2:ReplaceRoute"
        ],
        "Resource": "*"
      },
      {
        "Sid": "NetworkSecurityGroupRules",
        "Effect": "Allow",
        "Action": [
          "ec2:AuthorizeSecurityGroupIngress",
          "ec2:RevokeSecurityGroupIngress"
        ],
        "Resource": "*"
      },
      {
        "Sid": "NetworkPeeringAccept",
        "Effect": "Allow",
        "Action": [
          "ec2:AcceptVpcPeeringConnection",
          "ec2:DeleteVpcPeeringConnection",
          "ec2:ModifyVpcPeeringConnectionOptions"
        ],
        "Resource": "*"
      },
      {
        "Sid": "NetworkSecurityGroupCreate",
        "Effect": "Allow",
        "Action": "ec2:CreateSecurityGroup",
        "Resource": "arn:aws:ec2:*:*:security-group/*",
        "Condition": { "StringEquals": { "aws:RequestTag/ManagedBy": `${prefix}-agent` } }
      },
      {
        "Sid": "NetworkSecurityGroupCreateInVpc",
        "Effect": "Allow",
        "Action": "ec2:CreateSecurityGroup",
        "Resource": "arn:aws:ec2:*:*:vpc/*"
      },
      {
        "Sid": "NetworkSecurityGroupDelete",
        "Effect": "Allow",
        "Action": "ec2:DeleteSecurityGroup",
        "Resource": "*",
        "Condition": { "StringEquals": { "aws:ResourceTag/ManagedBy": `${prefix}-agent` } }
      },
      {
        "Sid": "Ec2TagOnCreate",
        "Effect": "Allow",
        "Action": "ec2:CreateTags",
        "Resource": "*",
        "Condition": {
          "StringEquals": { "aws:RequestTag/ManagedBy": `${prefix}-agent` },
          "StringLike": { "ec2:CreateAction": ["CreateSecurityGroup"] }
        }
      },
      {
        "Sid": "LatticeCreate",
        "Effect": "Allow",
        "Action": [
          "vpc-lattice:CreateResourceGateway",
          "vpc-lattice:CreateResourceConfiguration"
        ],
        "Resource": "*",
        "Condition": { "StringEquals": { "aws:RequestTag/ManagedBy": `${prefix}-agent` } }
      },
      {
        "Sid": "LatticeTagOnCreate",
        "Effect": "Allow",
        "Action": "vpc-lattice:TagResource",
        "Resource": "*",
        "Condition": {
          "StringEquals": { "aws:RequestTag/ManagedBy": `${prefix}-agent` },
          "StringLike": { "vpc-lattice:CreateAction": ["CreateResourceGateway", "CreateResourceConfiguration"] }
        }
      },
      {
        "Sid": "LatticeManage",
        "Effect": "Allow",
        "Action": [
          "vpc-lattice:DeleteResourceGateway",
          "vpc-lattice:DeleteResourceConfiguration"
        ],
        "Resource": "*",
        "Condition": { "StringEquals": { "aws:ResourceTag/ManagedBy": `${prefix}-agent` } }
      },
      {
        "Sid": "LatticeGatewayNetworkInterfaces",
        "Effect": "Allow",
        "Action": [
          "ec2:CreateNetworkInterface",
          "ec2:CreateNetworkInterfacePermission",
          "ec2:DeleteNetworkInterface",
          "ec2:AssignPrivateIpAddresses",
          "ec2:UnassignPrivateIpAddresses",
          "ec2:ModifyNetworkInterfaceAttribute"
        ],
        "Resource": "*",
        "Condition": { "Bool": { "aws:ViaAWSService": "true" } }
      },
      {
        "Sid": "LatticeServiceLinkedRole",
        "Effect": "Allow",
        "Action": "iam:CreateServiceLinkedRole",
        "Resource": "arn:aws:iam::*:role/aws-service-role/vpc-lattice.amazonaws.com/AWSServiceRoleForVpcLattice",
        "Condition": { "StringEquals": { "iam:AWSServiceName": "vpc-lattice.amazonaws.com" } }
      },
      {
        "Sid": "ResourceShareCreate",
        "Effect": "Allow",
        "Action": "ram:CreateResourceShare",
        "Resource": "*",
        "Condition": {
          "StringEquals": {
            "aws:RequestTag/ManagedBy": `${prefix}-agent`,
            "ram:RequestedResourceType": "vpc-lattice:ResourceConfiguration"
          }
        }
      },
      {
        "Sid": "ResourceShareDelete",
        "Effect": "Allow",
        "Action": "ram:DeleteResourceShare",
        "Resource": "*",
        "Condition": { "StringEquals": { "aws:ResourceTag/ManagedBy": `${prefix}-agent` } }
      },
      {
        "Sid": "ReadOwnRoleTags",
        "Effect": "Allow",
        "Action": "iam:ListRoleTags",
        "Resource": `arn:aws:iam::*:role/${prefix}-agent-*`
      },
      {
        "Sid": "ResourceExplorer",
        "Effect": "Allow",
        "Action": [
          "resource-explorer-2:Search",
          "resource-explorer-2:ListIndexes",
          "resource-explorer-2:GetIndex",
          "resource-explorer-2:CreateIndex",
          "resource-explorer-2:UpdateIndexType"
        ],
        "Resource": "*"
      }
    ]
  }
}

// ── Configure Database Wizard ──────────────────────────────────────────────
// Two-step modal opened from a discovered database row.
// Step 1: copy-paste templates for creating a read-only DB user + Secrets Manager secret.
// Step 2: paste the secret ARN, run a Test Connection pre-flight, then Configure.
function ConfigureDatabaseModal({ space, state, update, onClose, onTest, onSubmit, copyHelper, copiedField }) {
  const { db, step, secretArn, dbName, testing, testResult, submitting, error } = state
  const dbId = db.tags?.raw_identifier || db.display_name
  const region = db.region || 'us-east-1'
  const port = db.port || 5432
  const endpoint = db.endpoint || `<endpoint-not-yet-known>`
  const engine = (db.engine || 'postgres').includes('aurora') ? 'aurora-postgresql' : 'postgres'
  // Secrets live under the owning ADMIN's namespace, `<prefix>/<owner admin>/...`: the
  // admin's role (shared by all their spaces) reads exactly that prefix, so a secret one
  // admin created is unreadable through another admin's role, and a second space of the
  // same admin reuses it. The name is generated, so admins type nothing extra; a
  // hand-named secret outside it is refused at onboard.
  const secretName = `${config.resourcePrefix}/${ownerAdminOf(space)}/${engine}/${dbId}/readonly`

  // Step A is plain SQL, so it runs in psql or any GUI client. It generates the password
  // inside PostgreSQL (gen_random_uuid is cryptographically random, built in since PG 13)
  // and returns it as the last result row. Step B is one AWS CLI command with one line to
  // fill in; it can run anywhere the operator has credentials.
  const dbNameOrDefault = dbName || 'postgres'
  const hasEndpoint = !!db.endpoint
  const sqlSnippet = `-- Run as the master user, connected to database "${dbNameOrDefault}". Any SQL client.
-- Creates the read-only role dbops_ro. The last result row is its password.
DROP TABLE IF EXISTS dbops_ro_password;
CREATE TEMP TABLE dbops_ro_password AS
  SELECT replace(gen_random_uuid()::text, '-', '') AS password;

DO $$
DECLARE
  pwd text := (SELECT password FROM dbops_ro_password);
  r record;
  ext_schema text;
BEGIN
  IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'dbops_ro') THEN
    EXECUTE format('ALTER ROLE dbops_ro WITH LOGIN PASSWORD %L', pwd);
  ELSE
    EXECUTE format('CREATE ROLE dbops_ro LOGIN PASSWORD %L', pwd);
  END IF;
  GRANT pg_monitor TO dbops_ro;
  EXECUTE format('GRANT CONNECT ON DATABASE %I TO dbops_ro', '${dbNameOrDefault.replace(/'/g, "''")}');

  -- SELECT on every non-system schema, including tables created later.
  FOR r IN
    SELECT nspname FROM pg_namespace
    WHERE nspname NOT IN ('pg_catalog','information_schema','pg_toast')
      AND nspname NOT LIKE 'pg_temp_%'
      AND nspname NOT LIKE 'pg_toast_temp_%'
  LOOP
    EXECUTE format('GRANT USAGE ON SCHEMA %I TO dbops_ro', r.nspname);
    EXECUTE format('GRANT SELECT ON ALL TABLES IN SCHEMA %I TO dbops_ro', r.nspname);
    EXECUTE format('GRANT SELECT ON ALL SEQUENCES IN SCHEMA %I TO dbops_ro', r.nspname);
    EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA %I GRANT SELECT ON TABLES TO dbops_ro', r.nspname);
    EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA %I GRANT SELECT ON SEQUENCES TO dbops_ro', r.nspname);
  END LOOP;

  -- Bloat analysis functions, when the pgstattuple extension is installed.
  SELECT extnamespace::regnamespace::text INTO ext_schema
    FROM pg_extension WHERE extname = 'pgstattuple';
  IF ext_schema IS NOT NULL THEN
    EXECUTE format('GRANT EXECUTE ON FUNCTION %s.pgstattuple_approx(regclass) TO dbops_ro', ext_schema);
    EXECUTE format('GRANT EXECUTE ON FUNCTION %s.pgstattuple(regclass) TO dbops_ro', ext_schema);
  END IF;
END $$;

SELECT password AS dbops_ro_password FROM dbops_ro_password;`

  // The password is 32 hex characters, so it needs no JSON escaping. Without a known
  // endpoint, host is left out of the secret, which onboarding accepts.
  const hostField = hasEndpoint ? `\\"host\\":\\"${endpoint}\\",` : ''
  const secretString = `"{\\"username\\":\\"dbops_ro\\",\\"password\\":\\"$DBOPS_RO_PASSWORD\\",\\"engine\\":\\"postgres\\",${hostField}\\"port\\":${port},\\"dbname\\":\\"${dbNameOrDefault}\\"}"`

  // The { } group makes the shell read the whole paste before running it, so the prompt
  // waits for the operator instead of consuming the next pasted line; nothing may follow
  // the closing brace for the same reason. read -s keeps the password off the screen and
  // out of shell history. printf rather than read -p: in zsh -p means something else.
  const cliSnippet = `# Asks for the Step A password, then prints the secret ARN for Step 2.
{
printf 'Password from Step A: '; read -rs DBOPS_RO_PASSWORD; echo
aws secretsmanager create-secret \\
  --name "${secretName}" \\
  --description "Read-only credentials used by ${APP_NAME}" \\
  --secret-string ${secretString} \\
  --tags Key=${config.resourcePrefix}:purpose,Value=readonly Key=${config.resourcePrefix}:db_identifier,Value=${dbId} \\
  --region ${region}
unset DBOPS_RO_PASSWORD
}`

  const rotateSnippet = `{
printf 'Password from Step A: '; read -rs DBOPS_RO_PASSWORD; echo
aws secretsmanager put-secret-value \\
  --secret-id "${secretName}" \\
  --secret-string ${secretString} \\
  --region ${region}
unset DBOPS_RO_PASSWORD
}`
  const cloudShellUrl = `https://${region}.console.aws.amazon.com/cloudshell/home?region=${region}`

  const arnLooksValid = /^arn:aws[a-z\-]*:secretsmanager:[a-z0-9\-]+:\d{12}:secret:/.test(secretArn.trim())
  // Shape is not enough. The owner admin's role reads secrets named under the admin's
  // folder `<resourcePrefix>/<owner admin>/` only (the server refuses anything else at
  // Test and at onboard, see _secret_namespace_refusal), so a correctly formed ARN
  // naming a secret outside that folder is one this space can never use.
  const secretFolder = `${config.resourcePrefix}/${ownerAdminOf(space)}/`
  const typedSecretName = (secretArn.trim().split(':secret:')[1] || '').replace(/-[A-Za-z0-9]{6}$/, '')
  const prefixOk = typedSecretName.startsWith(secretFolder)

  const footer = (
    <div className="flex items-center justify-between">
          <button onClick={onClose} className="text-sm text-fg-2 hover:text-fg">Cancel</button>
          <div className="flex items-center gap-2">
            {step === 2 && (
              <button onClick={() => update({ step: 1 })}
                className="px-4 py-2 text-sm text-fg-2 border border-line rounded-lg hover:bg-surface-3">
                Previous
              </button>
            )}
            {step === 1 && (
              <button onClick={() => update({ step: 2 })}
                className="px-4 py-2 text-sm font-medium bg-primary text-primary-fg rounded-lg hover:bg-primary-hover">
                Next
              </button>
            )}
            {step === 2 && (
              <button onClick={onTest}
                disabled={testing || submitting || !arnLooksValid}
                className="px-4 py-2 text-sm font-medium text-primary border border-primary rounded-lg hover:bg-primary-subtle disabled:opacity-50">
                {testing ? 'Testing…' : 'Test secret'}
              </button>
            )}
            {/* Gated on a clean test, not just a well-formed ARN. A warning here means
                Configure is known to fail, so the button is withheld and says why —
                rather than letting an operator click past the warnings and hit a failure
                minutes later from the background network task, where the cause is a line
                in config_log rather than anything on screen. */}
            {step === 2 && (
              <button onClick={onSubmit}
                disabled={submitting || !arnLooksValid || !testResult?.ok || !!testResult?.warning}
                aria-describedby={!testResult?.ok ? 'configure-gate-hint' : undefined}
                title={!testResult?.ok
                  ? 'Run Test secret first'
                  : testResult?.warning ? 'Resolve the warning above first' : ''}
                className="inline-flex items-center gap-1.5 px-4 py-2 text-sm font-medium bg-primary text-primary-fg rounded-lg hover:bg-primary-hover disabled:opacity-50 disabled:cursor-not-allowed">
                {submitting ? 'Onboarding…' : <><Settings2 className="w-4 h-4" aria-hidden /> {db.onboarded_elsewhere ? 'Add to this space' : 'Onboard'}</>}
              </button>
            )}
          </div>
    </div>
  )

  return (
    <SideSheet title={db.onboarded_elsewhere ? 'Add database to this space' : 'Onboard database'} onClose={onClose} footer={footer}
      subtitle={<><code className="bg-surface-3 px-1 rounded">{db.display_name}</code> · {engineLabel(db.engine)} · {region}{db.account_id && <> · account {db.account_id}</>}</>}
      header={<StepIndicator step={step} labels={['Create read-only user & secret', 'Connect & test']} />}>
        {/* Step 1 */}
        {step === 1 && (
          <div className="px-6 py-4 space-y-5">
            <p className="text-xs text-fg-3">
              The agent connects with a dedicated read-only user. Already have a secret for it? Skip to Step 2.
            </p>

            <div>
              <div className="flex items-center justify-between mb-1">
                <p className="text-xs font-medium text-fg-2">Step A: Create the read-only user (SQL, run as the master user)</p>
                <button onClick={() => copyHelper(sqlSnippet, 'sql')}
                  className="text-[10px] text-primary hover:underline flex items-center gap-1">{copiedField === 'sql' ? <><Check className="w-3 h-3" aria-hidden /> Copied</> : <><Copy className="w-3 h-3" aria-hidden /> Copy</>}</button>
              </div>
              <pre className="bg-surface-code text-ok rounded p-3 text-[10px] overflow-x-auto max-h-48">{sqlSnippet}</pre>
              <p className="text-[10px] text-fg-muted mt-1">The last result row is the password. Keep it for Step B.</p>
            </div>

            <div>
              <div className="flex items-center justify-between mb-1">
                <p className="text-xs font-medium text-fg-2">Step B: Store the credentials in Secrets Manager (AWS CLI)</p>
                <div className="flex items-center gap-3">
                  <a href={cloudShellUrl} target="_blank" rel="noopener noreferrer"
                    className="text-[10px] text-primary hover:underline inline-flex items-center gap-1" title="Opens CloudShell for whichever account you are signed in to">
                    <ExternalLink className="w-3 h-3" aria-hidden /> Open CloudShell
                  </a>
                  <button onClick={() => copyHelper(cliSnippet, 'cli')}
                    className="text-[10px] text-primary hover:underline flex items-center gap-1">{copiedField === 'cli' ? <><Check className="w-3 h-3" aria-hidden /> Copied</> : <><Copy className="w-3 h-3" aria-hidden /> Copy</>}</button>
                </div>
              </div>
              <p role="note" className="text-[11px] font-medium text-warn-text bg-warn-bg border border-warn-border rounded p-2 mb-1">
                Paste the whole block. It asks for the Step A password when it runs.
              </p>
              <pre className="bg-surface-code text-ok rounded p-3 text-[10px] overflow-x-auto max-h-48">{cliSnippet}</pre>
              <p className="text-[10px] text-fg-muted mt-1">
                Run in CloudShell signed in to account {db.account_id || 'of the database'}, or any shell with credentials for it. Keep the secret name as shown.
              </p>
              <details className="mt-2 rounded-lg border border-line">
                <summary className="px-3 py-2 text-[11px] font-medium text-fg-2 cursor-pointer hover:bg-surface-2 rounded-lg select-none">
                  Secret already exists? Update it instead
                </summary>
                <div className="px-3 pb-3">
                  <div className="flex justify-end mb-1">
                    <button onClick={() => copyHelper(rotateSnippet, 'rotate')}
                      className="text-[10px] text-primary hover:underline flex items-center gap-1">{copiedField === 'rotate' ? <><Check className="w-3 h-3" aria-hidden /> Copied</> : <><Copy className="w-3 h-3" aria-hidden /> Copy</>}</button>
                  </div>
                  <pre className="bg-surface-code text-ok rounded p-3 text-[10px] overflow-x-auto max-h-48">{rotateSnippet}</pre>
                </div>
              </details>
            </div>
          </div>
        )}

        {/* Step 2 */}
        {step === 2 && (
          <div className="px-6 py-4 space-y-4">
            {db.suggested_secret_arn ? (
              <div className="bg-info-bg border border-info-border rounded-lg p-3 text-xs text-info-text">
                <span className="font-medium">Credential from your space {db.suggested_secret_from}.</span>{' '}
                Your spaces share one secret namespace, so this space can use the same read-only user. Click Test,
                then Add. To use a different read-only user instead, replace the ARN below.
              </div>
            ) : db.onboarded_elsewhere && (
              <div className="bg-info-bg border border-info-border rounded-lg p-3 text-xs text-info-text">
                <span className="font-medium">Already running in {db.shared_with_count} other space{db.shared_with_count > 1 ? 's' : ''}.</span>{' '}
                Onboarding here verifies that this space's role can read the secret you provide and that it belongs to this database,
                then adds the database to this space. The network path, collection schedules and scan history are shared and stay as they are.
              </div>
            )}
            <div>
              <label className="block text-xs font-medium text-fg-2 mb-1">Secret ARN *</label>
              <input
                type="text"
                value={secretArn}
                onChange={e => update({ secretArn: e.target.value, testResult: null })}
                className="w-full px-3 py-2 text-sm font-mono border border-line-strong rounded-lg focus:outline-none focus:ring-2 focus:ring-primary"
                placeholder={`arn:aws:secretsmanager:${region}:${db.account_id || '<account>'}:secret:${secretName}-XXXXXX`}
              />
              {!arnLooksValid && secretArn.trim() && (
                <p className="text-[10px] text-warn-mid mt-1">That doesn't look like a Secrets Manager ARN.</p>
              )}
              {arnLooksValid && !prefixOk && (
                <div role="alert" className="text-[11px] text-warn-text bg-warn-bg border border-warn-border rounded p-2 mt-1 space-y-1">
                  <p>
                    <span className="font-medium">This secret will be refused.</span> It is named{' '}
                    <code className="bg-surface-3 px-1 rounded break-all">{typedSecretName || 'unknown'}</code>, but this
                    space's access role can only read secrets whose name starts with{' '}
                    <code className="bg-surface-3 px-1 rounded break-all">{secretFolder}</code>.
                  </p>
                  <p>
                    Create it with the name from Step 1:{' '}
                    <code className="bg-surface-3 px-1 rounded break-all">{secretName}</code>
                  </p>
                </div>
              )}
            </div>

            <div>
              <label className="block text-xs font-medium text-fg-2 mb-1">Database name</label>
              <input
                type="text"
                value={dbName}
                onChange={e => update({ dbName: e.target.value, testResult: null })}
                className="w-full px-3 py-2 text-sm font-mono border border-line-strong rounded-lg focus:outline-none focus:ring-2 focus:ring-primary"
                placeholder="postgres" />
            </div>

            {!testResult && (
              <p id="configure-gate-hint" className="text-xs text-fg-3 bg-surface-2 border border-line rounded-lg px-3 py-2">
                Run <span className="font-medium text-fg-2">Test secret</span> first. Configure is enabled once the secret can be read and points at this database.
              </p>
            )}

            {/* Three states, not two: a pass carrying a warning renders amber, not green,
                so a secret for the wrong database is not waved through. */}
            {testResult && (
              <div className={`rounded-lg p-3 text-xs ${
                !testResult.ok ? 'bg-err-bg border border-err-border text-err-text'
                  : testResult.warning ? 'bg-warn-bg border border-warn-border text-warn-text'
                    : 'bg-ok-bg border border-ok-border text-ok-text'}`}>
                {testResult.ok ? (
                  <>
                    <p>{testResult.warning
                      ? `⚠ Secret is readable, but: ${testResult.warning}`
                      : '✓ Secret validated — connectivity is verified during Configure'}</p>
                    {/* Printed because the host comes from the secret, not the instance:
                        seeing it is how an operator catches the wrong secret. */}
                    <p className="mt-1 font-mono text-[10px] opacity-80">
                      {testResult.user}@{testResult.host}:{testResult.port}/{testResult.database}
                    </p>
                  </>
                ) : `✕ ${testResult.stage && testResult.stage !== 'secret_namespace' ? testResult.stage + ': ' : ''}${testResult.error || 'Secret test failed'}`}
              </div>
            )}

            {error && (
              <div className="bg-err-bg border border-err-border rounded-lg p-3 text-xs text-err-text">{error}</div>
            )}
          </div>
        )}

    </SideSheet>
  )
}

function MonitoringCell({ api, db }) {
  const [t1Freq, setT1Freq] = useState(db.tier1_frequency || '5min')
  const [t2Freq, setT2Freq] = useState(db.tier2_frequency || 'daily')
  const [t1On, setT1On] = useState(db.tier1_enabled !== false)
  const [t2On, setT2On] = useState(db.tier2_enabled !== false)

  // Re-sync when db prop changes (after fetchDatabases)
  useEffect(() => {
    setT1Freq(db.tier1_frequency || '5min')
    setT2Freq(db.tier2_frequency || 'daily')
    setT1On(db.tier1_enabled !== false)
    setT2On(db.tier2_enabled !== false)
  }, [db.tier1_frequency, db.tier2_frequency, db.tier1_enabled, db.tier2_enabled])

  const t1Label = TIER1_OPTIONS.find(o => o.value === t1Freq)?.label || t1Freq
  const t2Label = TIER2_OPTIONS.find(o => o.value === t2Freq)?.label || t2Freq

  // Read-only in-space (monitoring-config): Tier 1/2 collection cadence is DB-level and
  // owned by super admins in the Monitoring tab (collection is per physical DB, not per
  // space). Spaces display the effective cadence only.
  const Tile = ({ label, sub, on, value }) => (
    <div className="flex flex-col items-start rounded-md border border-line bg-surface-2 px-2 py-1 min-w-[74px]">
      <span className="text-[10px] uppercase tracking-wide text-fg-muted leading-none">{label}<span className="hidden sm:inline"> · {sub}</span></span>
      <span className={`mt-0.5 text-[11px] font-medium flex items-center gap-1 leading-none ${on ? 'text-ok' : 'text-fg-muted'}`}>
        <span className={`w-1.5 h-1.5 rounded-full ${on ? 'bg-ok' : 'bg-line-strong'}`} />
        {on ? value : 'Off'}
      </span>
    </div>
  )

  return (
    <div className="flex items-center gap-1.5" title="Collection cadence is managed by a super admin in the Monitoring tab">
      <Tile label="Tier 1" sub="Metrics" on={t1On} value={t1Label} />
      <Tile label="Tier 2" sub="Scan" on={t2On} value={t2Label} />
    </div>
  )

}

// A space is set up as one linear flow, and these five stages ARE the space's pages:
// the same list is the in-page stepper and the rail's sub-items, so there is no second
// place to look. Each stage unlocks when the one before it is done (stages 3 to 5 all
// need a database), and `SpaceDetail` reports the live done/locked state up through
// `onStages` so the rail can mark it too. The order is the order of work: connect
// accounts, bring in databases, (optionally) tune what the agent knows, let people in,
// then review the map.
export const SPACE_SECTIONS = [
  { id: 'accounts', label: 'Accounts & regions' },
  { id: 'databases', label: 'Databases' },
  { id: 'capabilities', label: 'Agent capabilities' },
  { id: 'operators', label: 'Operators' },
  { id: 'topology', label: 'Topology' },
]
// `tab="auto"` (what the space list opens with) resolves to the first stage that still
// needs work; a space whose setup is complete lands on Overview instead.
export const AUTO_TAB = 'auto'
// The read-only landing page of a set-up space: every stage's state, what needs
// attention, and the databases, in one place, each card linking into its stage. It is
// NOT a stage (nothing to complete, so it never counts toward "N of 5 done" and the
// stepper does not list it); the rail shows it first, before the stages.
export const SPACE_OVERVIEW_SECTION = { id: 'overview', label: 'Overview' }
// Pages of a space that are not setup stages: the stepper, StageNav, AUTO_TAB and the
// setup counters ignore them. The rail lists them after the stages for the callers who
// may open them (Audit: the space's creator or a super admin, the server's rule).
export const SPACE_EXTRA_SECTIONS = [{ id: 'audit', label: 'Audit' }]

// `canRename`: the display name is a DB-level label shared by every space that includes the
// database, so only a super admin may set it (same rule as the monitoring cadence).
// `canManage`: whether the caller may change this space (its creator, or a super admin).
// Membership of the space's group grants use only, so another admin added to it views it
// read-only; everything below the stage bar is rendered inside a disabled fieldset when
// this is false.
// `canViewAudit`: the caller may read this space's audit log (its creator or a super
// admin); `isSuperAdmin` widens the audit details to the super-admin fields.
export default function SpaceDetail({ api, space, onBack, onUpdate, tab: controlledTab, onTabChange, onStages, canRename = false, canManage = true, canViewAudit = false, isSuperAdmin = false }) {
  const [internalTab, setInternalTab] = useState(AUTO_TAB)
  const activeTab = controlledTab ?? internalTab
  const setActiveTab = (id) => { onTabChange ? onTabChange(id) : setInternalTab(id) }
  const [deleting, setDeleting] = useState(false)
  const [showAddAccount, setShowAddAccount] = useState(false)
  const [addAccountStep, setAddAccountStep] = useState(1)
  const [newAccountId, setNewAccountId] = useState('')
  const [newRoleName, setNewRoleName] = useState('')
  const [roleNameEditable, setRoleNameEditable] = useState(false)
  // The account check's answer for the id typed in: null, { loading }, { result } or { error }.
  const [accountCheck, setAccountCheck] = useState(null)
  const [addingAccount, setAddingAccount] = useState(false)
  const [addAccountError, setAddAccountError] = useState(null)
  const [copiedField, setCopiedField] = useState(null)
  const [editingRole, setEditingRole] = useState(null)
  const [editRoleValue, setEditRoleValue] = useState('')

  // Configuration state
  // Regions: one list per account, the primary included, answering the only question the
  // admin has here: which regions does each account have databases in? Seeded from the
  // saved per-account map, else the space-wide list, so a space saved before per-account
  // regions existed shows every account in the regions it was already scanning. Saved as
  // the server's account_regions (every account explicit) with `regions` as the union,
  // which the IAM step and older readers use. See _regions_for_account.
  const allAccountIds = () => [space.primary_account_id, ...(space.secondary_account_ids || [])].filter(Boolean)
  // Mirror the server (_regions_for_account): an account PRESENT in account_regions uses
  // its list as written — INCLUDING an explicit [] ("skip", e.g. the deployment account
  // with no databases). Only an account ABSENT from the map inherits the space-wide list,
  // so a space saved before per-account regions existed, and a freshly bound account, show
  // the regions they were already scanning. Treating a saved [] as "inherit" (the old
  // rule) re-expanded a deliberately empty account to the union, so "Regions changed"
  // could never clear after saving one.
  const seedAccountRegions = () => Object.fromEntries(allAccountIds().map(id => {
    const own = (space.account_regions || {})[id]
    return [id, Array.isArray(own) ? [...own] : [...(space.regions || [])]]
  }))
  const [accountRegions, setAccountRegions] = useState(seedAccountRegions)
  // Only auto-open rows during first-time setup (the space scans nowhere yet). Once some
  // account has regions, an empty account is a deliberate "skip" (e.g. the deployment
  // account with no databases), so we don't nag by re-opening it on every visit.
  const [editingRegions, setEditingRegions] = useState(() => {
    const seed = seedAccountRegions()
    const anyScanned = allAccountIds().some(id => seed[id].length)
    return new Set(anyScanned ? [] : allAccountIds().filter(id => !seed[id].length))
  })
  const [savingRegions, setSavingRegions] = useState(false)
  const [configuringAccess, setConfiguringAccess] = useState(false)
  const [accessConfigured, setAccessConfigured] = useState(false)
  const [accessError, setAccessError] = useState(null)

  // Discovery state
  const [discovering, setDiscovering] = useState(false)
  const [discoveredDbs, setDiscoveredDbs] = useState([])
  const [configuredDbs, setConfiguredDbs] = useState([])
  const [configuringDbs, setConfiguringDbs] = useState(new Set())
  const [loadingDbs, setLoadingDbs] = useState(false)
  const [deletingDbs, setDeletingDbs] = useState(new Set())
  const [editingName, setEditingName] = useState(null)

  // Configure Database wizard state
  const [configDbWizard, setConfigDbWizard] = useState(null) // { db, step, secretArn, dbName, testing, testResult, submitting, error }
  const [editNameValue, setEditNameValue] = useState('')
  const [configLog, setConfigLog] = useState([])
  const [testingConnection, setTestingConnection] = useState(null)
  const [connectionResults, setConnectionResults] = useState({})
  const [selectedMonDbs, setSelectedMonDbs] = useState(new Set())
  const [bulkMonConfig, setBulkMonConfig] = useState(null) // {tier1_frequency, tier2_frequency, ...}
  const [applyingBulk, setApplyingBulk] = useState(false)

  // Topology state
  const [topology, setTopology] = useState(null)
  const [topologyLoading, setTopologyLoading] = useState(false)
  const [topologyRefreshing, setTopologyRefreshing] = useState(false)
  const [dbFilter, setDbFilter] = useState('all')
  const [dbsLoaded, setDbsLoaded] = useState(false)
  // Operator count drives the Operators stage's status. Fetched here once so the stepper
  // knows it before that stage is ever opened; WebAppUsers keeps it current afterwards.
  const [operatorCount, setOperatorCount] = useState(0)
  const topologyStats = topology?.stats || null

  const webAppUrl = WEBAPP_BASE_URL ? `${WEBAPP_BASE_URL}?space_id=${space.space_id}` : ''

  useEffect(() => {
    api.get(`/api/spaces/${space.space_id}/users`)
      .then(d => setOperatorCount((d?.users || []).length))
      .catch(() => {})
  }, [space.space_id])

  // Fetch databases for this space
  const fetchDatabases = async () => {
    setLoadingDbs(true)
    try {
      // Two lists, two meanings. MEMBERS are the databases this space has onboarded (the
      // server scopes /api/databases?space_id= by member_space_ids): they are the space's
      // databases, shown in the main table and to its operators. CANDIDATES are the
      // databases discovery merely found for this space (/candidates): they grant
      // nothing until an admin onboards one with a secret, so they live only inside the
      // Discovery step. A database another space already runs appears as a candidate
      // flagged onboarded_elsewhere; onboarding it here reuses the existing network path.
      const [members, candidates] = await Promise.all([
        api.get(`/api/databases?space_id=${space.space_id}`),
        api.get(`/api/spaces/${space.space_id}/candidates`).catch(() => []),
      ])
      setConfiguredDbs(members.filter(d => d.status && d.status !== 'discovered').map(showStalledAsFailed))
      setDiscoveredDbs(Array.isArray(candidates) ? candidates.map(showStalledAsFailed) : [])
    } catch {}
    finally { setLoadingDbs(false); setDbsLoaded(true) }
  }

  useEffect(() => { fetchDatabases() }, [])

  // Fetch the topology graph (stats feed the header tiles, the rest the Topology tab)
  const fetchTopologyStats = async () => {
    setTopologyLoading(true)
    try {
      const data = await api.get(`/api/spaces/${space.space_id}/topology`)
      setTopology(data && data.stats ? data : null)
    } catch { setTopology(null) }
    finally { setTopologyLoading(false) }
  }
  useEffect(() => { if (space.access_configured) fetchTopologyStats() }, [space.access_configured])

  // Ask the backend to re-collect RDS/CloudWatch/network metadata, then re-read the graph.
  // The collection runs as a background task, so give it a moment before re-fetching.
  const refreshTopology = async () => {
    setTopologyRefreshing(true)
    try { await api.post(`/api/spaces/${space.space_id}/topology/refresh`) } catch {}
    // The collection runs as a background task over every database in series, so a single
    // early re-read could finish and clear the spinner while
    // later databases were still being collected. Re-read a few times with backoff so
    // late-landing metadata appears on its own; the sequence is bounded, so it always
    // ends and never polls forever.
    for (const wait of [2000, 3000, 5000]) {
      await new Promise(r => setTimeout(r, wait))
      await fetchTopologyStats()
    }
    setTopologyRefreshing(false)
  }

  // Poll while any DB is "configuring" or "deleting".
  // A first onboard stays a candidate (under Discover) while its network is built and
  // its login verified, and becomes a member only then, so poll both lists. A removal
  // runs after the request returns, so poll until the row is gone (or Delete failed).
  useEffect(() => {
    const hasConfiguring = [...configuredDbs, ...discoveredDbs].some(d => d.status === 'configuring' || d.status === 'deleting')
    if (!hasConfiguring) return
    const interval = setInterval(fetchDatabases, 3000)
    return () => clearInterval(interval)
  }, [configuredDbs, discoveredDbs])

  // The topology is built from the registry on every GET, so it is current whenever it
  // is read. Read it again when the space's onboarded databases change (an onboard
  // finishing, a removal completing) and whenever the Topology page is opened, so the
  // map never shows the state from before the last onboard.
  const onboardedSignature = configuredDbs.map(d => `${d.instance_id}:${d.status}`).sort().join(',')
  useEffect(() => {
    if (space.access_configured && dbsLoaded) fetchTopologyStats()
  }, [onboardedSignature])
  useEffect(() => {
    if (activeTab === 'topology' && space.access_configured) fetchTopologyStats()
  }, [activeTab])

  // Check if access is already configured (per-space flag)
  useEffect(() => {
    if (space.access_configured) {
      setAccessConfigured(true)
    }
  }, [space.access_configured])

  // Step 1: Save regions. Every account is explicit in account_regions; `regions` is the
  // union, so nothing that reads the space-wide list (IAM step, overview counts) changes.
  const regionsFor = id => accountRegions[id] || []
  const regionPayload = () => {
    const map = Object.fromEntries(allAccountIds().map(id => [id, [...regionsFor(id)].sort()]))
    const union = [...new Set(Object.values(map).flat())].sort()
    return { regions: union, account_regions: map }
  }

  const handleSaveRegions = async () => {
    setSavingRegions(true)
    try {
      const updated = await api.put(`/api/spaces/${space.space_id}`, regionPayload())
      onUpdate(updated)
    } catch (e) { alert(e.detail || 'Failed to save') }
    finally { setSavingRegions(false) }
  }

  // Step 2: Configure access (Resource Explorer + validate accounts)
  const handleConfigureAccess = async () => {
    setConfiguringAccess(true)
    setAccessError(null)
    try {
      // Save regions (default + per-account overrides) first
      await api.put(`/api/spaces/${space.space_id}`, regionPayload())
      const result = await api.post(`/api/spaces/${space.space_id}/configure-access`)
      if (result.status === 'configured') {
        setAccessConfigured(true)
        setAccessError(null)
      } else if (result.status === 'incomplete') {
        setAccessConfigured(false)
        setAccessError(result.errors?.join('\n') || 'Some accounts could not be validated.')
      }
      const updated = await api.get(`/api/spaces/${space.space_id}`)
      onUpdate(updated)
    } catch (e) {
      setAccessError(e.detail || e.message || 'Configuration failed')
    } finally { setConfiguringAccess(false) }
  }

  // Repair a host-route database's /32s now (the same repair the API task runs in the
  // background); the result lands in the config log, and the row refreshes.
  const [repairingRoutes, setRepairingRoutes] = useState(new Set())
  const handleRouteRepair = async (db) => {
    setRepairingRoutes(prev => new Set(prev).add(db.instance_id))
    try {
      const r = await api.post(`/api/databases/${db.instance_id}/route-repair`)
      setConfigLog(prev => [...prev, `Route repair ${db.display_name}: ${r.message}`])
      await fetchDatabases()
    } catch (e) {
      setConfigLog(prev => [...prev, `Route repair ${db.display_name} failed: ${e.detail || e.message}`])
    }
    setRepairingRoutes(prev => { const n = new Set(prev); n.delete(db.instance_id); return n })
  }

  // Step 3: Run discovery
  const [discoveryResult, setDiscoveryResult] = useState(null)
  // What the last discovery found but cannot onboard: this run's answer, else the space's.
  const notSupported = discoveryResult?.not_supported || space.unsupported_databases || []

  // Discovery scope: which of the space's accounts to scan. Empty set = all. Lets an admin
  // of a many-account space re-scan just the account they added, instead of the fleet.
  // Rows in accounts a scoped run skips are never retired (the server retires only within
  // the pairs it actually scanned), so a narrow scan is always safe.
  const spaceAccountIds = [space.primary_account_id, ...(space.secondary_account_ids || [])].filter(Boolean)
  const [discoverAccounts, setDiscoverAccounts] = useState(new Set())
  const toggleDiscoverAccount = id => setDiscoverAccounts(prev => { const n = new Set(prev); if (n.has(id)) n.delete(id); else n.add(id); return n })
  const discoverScopeLabel = discoverAccounts.size === 0 || discoverAccounts.size === spaceAccountIds.length
    ? 'All accounts' : `${discoverAccounts.size} of ${spaceAccountIds.length} accounts`

  const handleDiscover = async () => {
    setDiscovering(true)
    setDiscoveryResult(null)
    try {
      // Discovery now runs in the background (it can outlast the gateway timeout). Kick it
      // off, then poll the status until it finishes; refresh the lists on completion.
      const scoped = discoverAccounts.size > 0 && discoverAccounts.size < spaceAccountIds.length
      await api.post(`/api/spaces/${space.space_id}/discover`, scoped ? { account_ids: [...discoverAccounts] } : {})
      const poll = async () => {
        for (let i = 0; i < 150; i++) {   // up to ~5 min at 2 s
          await new Promise(r => setTimeout(r, 2000))
          let st
          try { st = (await api.get(`/api/spaces/${space.space_id}/discovery-status`)).discovery_status }
          catch { continue }
          if (st?.state === 'done') return st
          if (st?.state === 'failed') throw new Error(st.message || 'Discovery failed')
        }
        throw new Error('Discovery is taking longer than expected; check back shortly.')
      }
      const status = await poll()
      setDiscoveryResult(status)
      await fetchDatabases()
      const updated = await api.get(`/api/spaces/${space.space_id}`)
      onUpdate(updated)
    } catch (e) { alert(e.detail || e.message || 'Discovery failed') }
    finally { setDiscovering(false) }
  }

  const openConfigDbWizard = (db) => {
    setConfigDbWizard({
      db,
      // A candidate another of this admin's spaces already holds comes with that space's
      // ARN prefilled and opens on Connect & test: two clicks, Test and Add. Otherwise the
      // wizard starts with the user-and-secret instructions.
      step: db.suggested_secret_arn ? 2 : 1,
      secretArn: db.suggested_secret_arn || db.secret_arn || '',
      dbName: db.database_name || 'postgres',
      testing: false,
      testResult: null,
      submitting: false,
      error: null,
    })
  }
  const closeConfigDbWizard = () => setConfigDbWizard(null)
  const updateWizard = (patch) => setConfigDbWizard(prev => prev ? { ...prev, ...patch } : prev)

  const handleTestCredentials = async () => {
    if (!configDbWizard) return
    updateWizard({ testing: true, testResult: null, error: null })
    try {
      const result = await api.post(
        `/api/spaces/${space.space_id}/databases/${configDbWizard.db.instance_id}/test-credentials`,
        { secret_arn: configDbWizard.secretArn.trim(), database_name: configDbWizard.dbName.trim() || null },
      )
      updateWizard({ testResult: result, testing: false })
    } catch (e) {
      updateWizard({ testing: false, error: e.detail || e.message || 'Test failed' })
    }
  }

  const handleSubmitConfigDb = async () => {
    if (!configDbWizard) return
    updateWizard({ submitting: true, error: null })
    try {
      await api.post(
        `/api/spaces/${space.space_id}/databases/${configDbWizard.db.instance_id}/configure`,
        { secret_arn: configDbWizard.secretArn.trim(), database_name: configDbWizard.dbName.trim() || null },
      )
      closeConfigDbWizard()
      await fetchDatabases()
    } catch (e) {
      updateWizard({ submitting: false, error: e.detail || e.message || 'Configure failed' })
    }
  }

  const handleDelete = async () => {
    if (!confirm(`Delete "${space.name}"? This cannot be undone.`)) return
    setDeleting(true)
    try { await api.del(`/api/spaces/${space.space_id}`); onBack() }
    catch (e) { alert(e.detail || 'Failed to delete') }
    finally { setDeleting(false) }
  }

  // Removing a database here acts on THIS space only: it untags the space from the
  // physical database. Other spaces that include it keep it, its network path and its
  // history; the database is torn down only when the last space removes it. The space
  // id is sent explicitly so the server never has to guess which space the admin meant.
  const handleDeleteDb = async (instanceId, displayName) => {
    const db = [...configuredDbs, ...discoveredDbs].find(d => d.instance_id === instanceId)
    const others = Math.max(0, (db?.member_count ?? db?.member_space_ids?.length ?? 1) - 1)
    const prompt = others > 0
      ? `Remove "${displayName}" from this space? ${others} other space${others > 1 ? 's' : ''} still include${others > 1 ? '' : 's'} it and will keep monitoring it.`
      : `Remove "${displayName}"? This is the last space that includes it, so its network path, schedules and record will be removed.`
    if (!confirm(prompt)) return
    setDeletingDbs(prev => new Set([...prev, instanceId]))
    try {
      await api.del(`/api/databases/${instanceId}?space_id=${encodeURIComponent(space.space_id)}`)
      await fetchDatabases()
    } catch (e) { alert(e.detail || 'Failed to delete') }
    finally { setDeletingDbs(prev => { const n = new Set(prev); n.delete(instanceId); return n }) }
  }

  // Retry an unfinished removal ("Delete failed"): the same request again. Every
  // teardown step treats an already-deleted resource as done.
  const handleRetryRemoval = async (db, candidate) => {
    try {
      await api.del(candidate
        ? `/api/spaces/${space.space_id}/candidates/${db.instance_id}`
        : `/api/databases/${db.instance_id}?space_id=${encodeURIComponent(space.space_id)}`)
      await fetchDatabases()
    } catch (e) { alert(e.detail || 'Retry failed') }
  }

  // A candidate is not this space's database yet, so dismissing it is not a delete: it
  // only withdraws discovery's proposal for this space (it reappears on the next run).
  const handleDismissCandidate = async (instanceId, displayName) => {
    if (!confirm(`Hide "${displayName}" from this space's discovery results? It will show up again the next time you run discovery.`)) return
    try {
      await api.del(`/api/spaces/${space.space_id}/candidates/${instanceId}`)
      await fetchDatabases()
    } catch (e) { alert(e.detail || 'Failed to dismiss') }
  }

  // A DB-side route conflict the configurator would not resolve itself: the route's
  // holder is another live deployment's peering (or could not be classified), so
  // repointing it would cut that deployment off from this database. The admin takes
  // that decision explicitly, with the consequence spelled out; the server audits it and
  // its scope guard still refuses anything but the agent's own CIDR.
  const handleClaimRoute = async (db) => {
    const c = db.route_conflict || {}
    const far = c.far_vpc ? ` Its other end is agent VPC ${c.far_vpc}${c.far_account ? ` in account ${c.far_account}` : ''}, which is still active.` : ''
    const msg = `Claim the route for ${c.cidr} in ${c.route_table_id}?\n\n` +
      `It currently points to ${c.holder}.${far}\n` +
      `Reason it was not reclaimed automatically: ${c.detail || 'it could not be classified'}.\n\n` +
      `Claiming repoints it to this onboarding's peering ${c.peering_id}. Whatever still uses ${c.holder} ` +
      `will lose its connection to this database. Only continue if you are sure it is unused.`
    if (!confirm(msg)) return
    try {
      await api.post(`/api/spaces/${space.space_id}/databases/${db.instance_id}/claim-route`, {})
      await fetchDatabases()
      alert('Route repointed. Run Onboard/Configure again to verify connectivity.')
    } catch (e) { alert(e.detail || 'Could not claim the route') }
  }

  const handleRenameDb = async (instanceId) => {
    const trimmed = editNameValue.trim()
    if (!trimmed) { setEditingName(null); return }
    try {
      await api.put(`/api/databases/${instanceId}`, { display_name: trimmed })
      await fetchDatabases()
    } catch (e) { console.error('Rename failed:', e) }
    setEditingName(null)
  }

  const handleAddAccount = async () => {
    if (!newAccountId.trim() || !newRoleName.trim()) {
      setAddAccountError('Account ID and Role Name are required')
      return
    }
    setAddingAccount(true)
    setAddAccountError(null)
    try {
      const currentSecondary = space.secondary_account_ids || []
      const currentRoleNames = space.secondary_role_names || {}
      await api.put(`/api/spaces/${space.space_id}`, {
        secondary_account_ids: [...currentSecondary, newAccountId.trim()],
        secondary_role_names: { ...currentRoleNames, [newAccountId.trim()]: newRoleName.trim() },
      })
      const updated = await api.get(`/api/spaces/${space.space_id}`)
      onUpdate(updated)
      setShowAddAccount(false)
      setNewAccountId('')
      setNewRoleName('')
      setAddAccountStep(1)
    } catch (e) {
      setAddAccountError(e.detail || 'Failed to add account')
    } finally { setAddingAccount(false) }
  }

  const openAddAccount = () => {
    setShowAddAccount(true)
    setAddAccountStep(1)
    setNewAccountId('')
    setNewRoleName(suggestedRoleName(space))
    setRoleNameEditable(false)
    setAccountCheck(null)
    setAddAccountError(null)
  }

  const toggleAccountRegion = (id, r) => setAccountRegions(prev => {
    const cur = new Set(prev[id] || [])
    if (cur.has(r)) cur.delete(r); else cur.add(r)
    return { ...prev, [id]: [...cur] }
  })
  // "Apply to all accounts": copy one account's regions onto every account. This is what
  // a shared default was for, without asking the admin to learn a second concept.
  const applyRegionsToAll = id => setAccountRegions(prev => Object.fromEntries(allAccountIds().map(a => [a, [...(prev[id] || [])]])))
  const toggleEditingRegions = id => setEditingRegions(prev => {
    const n = new Set(prev); if (n.has(id)) n.delete(id); else n.add(id); return n
  })
  // An account added after this page loaded has no row yet: seed it from the primary's
  // regions (the most common answer) and open it so the admin confirms.
  useEffect(() => {
    const missing = allAccountIds().filter(id => !accountRegions[id])
    if (!missing.length) return
    setAccountRegions(prev => ({ ...prev, ...Object.fromEntries(missing.map(id => [id, [...(prev[space.primary_account_id] || [])]])) }))
    setEditingRegions(prev => new Set([...prev, ...missing]))
  }, [(space.secondary_account_ids || []).join(','), space.primary_account_id]) // eslint-disable-line react-hooks/exhaustive-deps
  // Every region any account is scanned in (the saved space-wide `regions`).
  const scannedRegions = [...new Set(allAccountIds().flatMap(regionsFor))].sort()

  // Detect if any account's regions changed from what's saved.
  const normRegionMap = m => JSON.stringify(Object.entries(m || {}).map(([k, rs]) => [k, [...rs].sort()]).sort())
  const regionsChanged = normRegionMap(regionPayload().account_regions) !== normRegionMap(seedAccountRegions())
    || scannedRegions.join(',') !== [...(space.regions || [])].sort().join(',')
  // An account with no regions is simply not scanned (the deployment account often has no
  // databases of its own). The only hard requirement is that the space scans SOMEWHERE —
  // so validation is gated on the union, not on every account individually.
  const scansNowhere = scannedRegions.length === 0

  // Setup progress is derived from the same flags that gate the buttons below —
  // never from a separate heuristic — so a step can't read "done" while its
  // action is still required (e.g. Reconfigure Access after a region change).
  const secondaryIds = space.secondary_account_ids || []
  const pendingSecondary = secondaryIds.filter(id => (space.account_statuses || {})[id] !== 'valid')
  const hasPendingAccounts = pendingSecondary.length > 0
  const needsReconfigure = !accessConfigured || regionsChanged || hasPendingAccounts
  // The main table shows only what this space has ONBOARDED (its members). Candidates
  // (found, not onboarded) are not the space's databases and live inside the Discover
  // step instead, so an operator-facing list and this admin list agree on what "in this
  // space" means.
  const allDbs = configuredDbs
  // The five stages of the space, in the order of work. "Done" is derived from the same
  // flags that gate the buttons inside each stage, never from a separate heuristic, so a
  // stage cannot read done while its action is still required.
  //   1 accounts      the primary account is known, regions chosen, and access validated
  //                   for every account in them (a pending secondary account is finished
  //                   here too, so it never locks anything)
  //   2 databases     this space has onboarded at least one database (candidates don't
  //                   count: discovery only proposes; a candidate is not this space's yet)
  //   3 capabilities  optional: the defaults work, so it reads done once databases exist,
  //                   with a nudge to review rather than a requirement
  //   4 operators     at least one operator can open this space in the operator app
  //   5 topology      the review step: done once a map has been generated
  const accessDone = !!space.primary_account_id && accessConfigured && !needsReconfigure
  const onboardDone = configuredDbs.length > 0
  const operatorsDone = operatorCount > 0
  const regionCount = scannedRegions.length
  const stages = SPACE_SECTIONS.map(s => {
    switch (s.id) {
      case 'accounts': return { ...s, done: accessDone, unlocked: true,
        summary: accessDone ? `${secondaryIds.length + 1} account${secondaryIds.length ? 's' : ''} · ${regionCount} region${regionCount > 1 ? 's' : ''} validated`
          : hasPendingAccounts ? `${pendingSecondary.length} account${pendingSecondary.length > 1 ? 's' : ''} pending validation`
            : !regionCount ? 'Choose regions, then validate access'
              : regionsChanged && accessConfigured ? 'Regions changed — validate again'
                : `${regionCount} region${regionCount > 1 ? 's' : ''} chosen · validate access` }
      case 'databases': return { ...s, done: onboardDone, unlocked: accessDone,
        summary: onboardDone
          ? `${configuredDbs.length} onboarded${discoveredDbs.length ? ` · ${discoveredDbs.length} found, not onboarded` : ''}`
          : discoveredDbs.length ? `${discoveredDbs.length} found — onboard the ones for this space`
            : 'Run discovery, then onboard with a read-only secret' }
      case 'capabilities': return { ...s, done: onboardDone, unlocked: onboardDone, optional: true,
        summary: onboardDone ? 'Defaults in use — review if this space needs its own knowledge or model' : 'Knowledge, skills and model for this space' }
      case 'operators': return { ...s, done: operatorsDone, unlocked: onboardDone,
        summary: operatorsDone ? `${operatorCount} operator${operatorCount > 1 ? 's' : ''} have access` : 'Nobody can open this space yet' }
      case 'topology': return { ...s, done: onboardDone && !!topology?.generated_at, unlocked: onboardDone,
        summary: topology?.generated_at ? `Map generated ${new Date(topology.generated_at).toLocaleDateString()}` : 'Review how the agent reaches each database' }
      default: return { ...s, done: false, unlocked: true, summary: '' }
    }
  })
  const setupDone = stages.filter(st => st.done).length
  const setupComplete = setupDone === stages.length
  const currentStageId = stages.find(st => !st.done)?.id || null
  // Let the rail mirror the stepper's state. Keyed on the rendered values so it only
  // fires when something the rail shows actually changes.
  const stagesKey = stages.map(st => `${st.id}:${st.done ? 1 : 0}${st.unlocked ? 1 : 0}`).join('|')
  useEffect(() => { onStages?.(stages) }, [stagesKey])
  // First open. A space with at least one onboarded database is a real space, so it
  // always lands on Overview: anything still open (no operators yet, no map, a failed
  // onboard) is listed there under "Needs attention" with a link into its stage, which
  // beats dropping the admin into that stage with no context. A space with no database
  // yet is still being created, so it keeps following the wizard: the first stage that
  // needs work (Accounts & regions, then Databases).
  useEffect(() => {
    if (activeTab !== AUTO_TAB || !dbsLoaded) return
    if (onboardDone) { setActiveTab(SPACE_OVERVIEW_SECTION.id); return }
    const first = stages.find(st => !st.done && st.unlocked && !st.optional)
    setActiveTab(first ? first.id : 'databases')
  }, [activeTab, dbsLoaded, stagesKey])
  const activeIdx = stages.findIndex(st => st.id === activeTab)
  const prevStage = activeIdx > 0 ? stages[activeIdx - 1] : null
  const nextStage = activeIdx >= 0 && activeIdx < stages.length - 1 ? stages[activeIdx + 1] : null
  // Footer of every stage: back (or Overview on the first stage), and forward once the next
  // stage is unlocked. The last stage ends the walk-through with Finish, which lands on
  // Overview. Stateless, so defining it per render is harmless.
  const goOverview = () => setActiveTab(SPACE_OVERVIEW_SECTION.id)
  const StageNav = () => (
    <div className="flex items-center justify-between gap-4">
      {prevStage ? (
        <button onClick={() => setActiveTab(prevStage.id)}
          className="inline-flex items-center gap-1.5 px-3 py-2 text-sm text-fg-2 border border-line rounded-lg hover:bg-surface-3 transition-colors">
          <ArrowLeft className="w-4 h-4" aria-hidden /> {prevStage.label}
        </button>
      ) : (
        <button onClick={goOverview}
          className="inline-flex items-center gap-1.5 px-3 py-2 text-sm text-fg-2 border border-line rounded-lg hover:bg-surface-3 transition-colors">
          <Home className="w-4 h-4" aria-hidden /> Overview
        </button>
      )}
      {nextStage ? (
        <div className="flex items-center gap-3">
          {!nextStage.unlocked && <span className="text-xs text-fg-muted">{stages[activeIdx]?.summary}</span>}
          <button onClick={() => setActiveTab(nextStage.id)} disabled={!nextStage.unlocked}
            title={nextStage.unlocked ? undefined : `Finish ${stages[activeIdx]?.label} first`}
            className="inline-flex items-center gap-1.5 px-4 py-2 text-sm font-medium bg-primary text-primary-fg rounded-lg hover:bg-primary-hover transition-colors disabled:opacity-50 disabled:cursor-not-allowed">
            Next: {nextStage.label} <ArrowRight className="w-4 h-4" aria-hidden />
          </button>
        </div>
      ) : (
        <div className="flex items-center gap-3">
          <span className="text-xs text-fg-3">{setupComplete ? 'Setup complete. Operators can open this space.' : `${setupDone} of ${stages.length} stages done.`}</span>
          <button onClick={goOverview}
            className="inline-flex items-center gap-1.5 px-4 py-2 text-sm font-medium bg-primary text-primary-fg rounded-lg hover:bg-primary-hover transition-colors">
            Finish: Overview <ArrowRight className="w-4 h-4" aria-hidden />
          </button>
        </div>
      )}
    </div>
  )
  const coverage = topology?.coverage || null
  const dbCounts = {
    all: allDbs.length,
    configured: configuredDbs.filter(d => d.status === 'configured').length,
    configuring: configuredDbs.filter(d => d.status === 'configuring').length,
    config_failed: configuredDbs.filter(d => d.status === 'config_failed').length,
    deleting: configuredDbs.filter(d => d.status === 'deleting').length,
    delete_failed: configuredDbs.filter(d => d.status === 'delete_failed').length,
  }
  const visibleDbs = dbFilter === 'all' ? allDbs : allDbs.filter(d => d.status === dbFilter)

  // Shared list controls (same search + engine/account/region filters + pager as the
  // operator app's Dashboard) for both database lists. The onboarded list keeps its
  // status chips as its status filter, so the toolbar's Status select is omitted there;
  // the found list gets a Status select over its onboarding states. Paging is by
  // replica group so a primary and its replicas stay on one page.
  const foundCtl = useDbListControls(discoveredDbs, {
    statusOf: d => d.status || 'discovered',
    statusOptions: [
      { value: 'discovered', label: 'Not onboarded' }, { value: 'configuring', label: 'Configuring' },
      { value: 'config_failed', label: 'Failed' }, { value: 'deleting', label: 'Deleting' },
      { value: 'delete_failed', label: 'Delete failed' },
    ],
    searchText: d => STATUS_BADGE[d.status]?.label || '',
  })
  const onboardedCtl = useDbListControls(visibleDbs, {
    searchText: d => STATUS_BADGE[d.status]?.label || '',
  })
  const foundNested = nestReplicas(foundCtl.filtered, d => d.replica_of)
  const foundPage = pageNested(foundNested, foundCtl.page, foundCtl.pageSize)
  const onboardedNested = nestReplicas(onboardedCtl.filtered, d => d.tags?.replica_of)
  const onboardedPage = pageNested(onboardedNested, onboardedCtl.page, onboardedCtl.pageSize)
  // Add-account wizard: the command is built from the space; the account id only says where to run it.
  const accountIdValid = /^\d{12}$/.test(newAccountId.trim())
  const memberRoleCli = memberRoleCliCommand(space, newRoleName, accountIdValid ? newAccountId.trim() : '*')
  const memberRoleUpdateCli = memberRolePolicyUpdateCommand(space, newRoleName, accountIdValid ? newAccountId.trim() : '*')
  const memberRoleTrustCli = memberRoleTrustUpdateCommand(space, newRoleName)
  // As soon as a full account id is typed (and again when the role name changes), ask the
  // server whether the owner's access role is already usable there. Debounced; a stale
  // answer for an id no longer in the box is dropped.
  const checkAccountId = showAddAccount && accountIdValid ? newAccountId.trim() : ''
  const checkRoleName = newRoleName.trim()
  useEffect(() => {
    if (!checkAccountId) { setAccountCheck(null); return }
    let cancelled = false
    setAccountCheck({ loading: true })
    const timer = setTimeout(async () => {
      try {
        const result = await api.post(`/api/spaces/${space.space_id}/accounts/check`,
          { account_id: checkAccountId, role_name: checkRoleName || undefined })
        if (!cancelled) setAccountCheck({ result })
      } catch (e) {
        if (!cancelled) setAccountCheck({ error: e.detail || 'Could not check the account.' })
      }
    }, 400)
    return () => { cancelled = true; clearTimeout(timer) }
  }, [checkAccountId, checkRoleName, space.space_id, api])
  const checkState = accountCheck?.result?.state || null
  const consoleRegion = config.awsRegion || (space.regions || [])[0] || 'us-east-1'
  const cloudShellUrl = `https://${consoleRegion}.console.aws.amazon.com/cloudshell/home?region=${consoleRegion}`
  const copyText = (value, key) => { navigator.clipboard.writeText(value); setCopiedField(key); setTimeout(() => setCopiedField(null), 2000) }

  return (
    <div>
      {/* Header */}
      <div className="flex items-start justify-between mb-6">
        <div>
          <h1 className="text-xl font-bold text-fg">{space.name}</h1>
          {space.description && <p className="text-sm text-fg-3 mt-1">{space.description}</p>}
          {space.owner && (
            <p className="text-xs text-fg-muted mt-1">
              Owner: <span className="text-fg-3 font-medium">{space.owner}</span>
            </p>
          )}
        </div>
        <div className="flex items-center gap-2">
          {/* The space's home, from any stage or the audit log. Hidden on Overview itself. */}
          {activeTab !== SPACE_OVERVIEW_SECTION.id && activeTab !== AUTO_TAB && (
            <button onClick={() => setActiveTab(SPACE_OVERVIEW_SECTION.id)}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium text-fg-2 border border-line rounded-lg hover:bg-surface-3 transition-colors">
              <Home className="w-4 h-4" aria-hidden /> Overview
            </button>
          )}
          {canViewAudit && (
            <button onClick={() => setActiveTab('audit')} aria-current={activeTab === 'audit' ? 'page' : undefined}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium text-fg-2 border border-line rounded-lg hover:bg-surface-3 transition-colors">
              <ScrollText className="w-4 h-4" aria-hidden /> Audit log
            </button>
          )}
          {webAppUrl && accessConfigured && configuredDbs.length > 0 && (
            <a href={webAppUrl} target="_blank" rel="noopener noreferrer"
              className="px-3 py-1.5 text-sm font-medium text-primary border border-primary rounded-lg hover:bg-primary-subtle transition-colors">
              Operator access ↗
            </a>
          )}
        </div>
      </div>

      {/* Status */}
      <div className="bg-surface rounded-lg border border-line px-6 py-4 mb-6">
        <div className="flex flex-wrap items-center gap-3">
          <span className={`px-2 py-0.5 rounded text-xs font-bold ${accessConfigured ? 'bg-ok text-white' : 'bg-warn text-warn-inverse'}`}>
            {accessConfigured ? 'Active' : 'Setup required'}
          </span>
          <span className="text-sm text-fg-2">
            {setupComplete
              ? `${APP_NAME} can discover and monitor the databases in this Agent Space.`
              : `This Agent Space sets the boundary of databases ${APP_NAME} can reach. Work through the steps below to start monitoring.`}
          </span>
          {topology?.generated_at && (
            <span className="text-xs text-fg-muted sm:ml-auto">Topology generated {new Date(topology.generated_at).toLocaleString()}</span>
          )}
        </div>
        <div className="mt-3 flex flex-wrap items-center gap-x-5 gap-y-1.5 text-xs text-fg-3">
          <span><span className="text-base font-semibold text-fg">{allDbs.length}</span> database{allDbs.length === 1 ? '' : 's'}</span>
          {coverage ? (
            <>
              {[['reachable', 'var(--ok-fg)'], ['pending', 'var(--warn-fg)'], ['blocked', 'var(--err-fg)'], ['unknown', 'var(--text-muted)']]
                .filter(([k]) => coverage[k]).map(([k, c]) => (
                  <span key={k} className="inline-flex items-center gap-1.5"><span className="w-2 h-2 rounded-full" style={{ backgroundColor: c }} /><span className="font-semibold text-fg-2">{coverage[k]}</span> {k}</span>
                ))}
              <span>metadata refreshed for <span className="font-semibold text-fg-2">{coverage.refreshed ?? 0}</span> of {coverage.databases ?? allDbs.length}</span>
            </>
          ) : (
            <>
              <span><span className="font-semibold text-fg-2">{configuredDbs.length}</span> configured</span>
              {discoveredDbs.length > 0 && <span className="text-warn-text"><span className="font-semibold">{discoveredDbs.length}</span> pending configuration</span>}
            </>
          )}
          <span><span className="font-semibold text-fg-2">{new Set([space.primary_account_id, ...secondaryIds].filter(Boolean)).size}</span> account{new Set([space.primary_account_id, ...secondaryIds].filter(Boolean)).size === 1 ? '' : 's'}</span>
          <span><span className="font-semibold text-fg-2">{(space.regions || []).length}</span> region{(space.regions || []).length === 1 ? '' : 's'}</span>
          {(topology?.warnings || []).length > 0 && (
            <button onClick={() => setActiveTab('topology')} className="inline-flex items-center gap-1 text-warn-text hover:underline">
              <AlertTriangle className="w-3.5 h-3.5" aria-hidden />{topology.warnings.length} warning{topology.warnings.length > 1 ? 's' : ''}
            </button>
          )}
        </div>
      </div>

      {/* ── The stepper: the space's five stages, in the order of work. This is the page's
             navigation (the rail's sub-items mirror it), so nobody has to notice the rail.
             A locked stage says what unlocks it; a done one shows a check. ── */}
      <nav aria-label="Space setup" className="bg-surface rounded-lg border border-line mb-6 overflow-hidden">
        <ol className="flex divide-x divide-line-2">
          {stages.map((st, i) => {
            const active = activeTab === st.id
            const isCurrent = st.id === currentStageId
            return (
              <li key={st.id} className="flex-1 min-w-0">
                <button onClick={() => st.unlocked && setActiveTab(st.id)} disabled={!st.unlocked} aria-current={active ? 'step' : undefined}
                  title={st.unlocked ? st.summary : `Unlocks after ${stages[i - 1]?.label || 'the previous stage'}`}
                  className={`w-full h-full flex items-start gap-3 px-4 py-3 text-left border-b-2 transition-colors ${
                    active ? 'border-primary bg-primary-subtle' : 'border-transparent'} ${
                    st.unlocked ? 'hover:bg-surface-2' : 'cursor-not-allowed'}`}>
                  <span className={`mt-0.5 w-6 h-6 rounded-full text-xs font-bold flex items-center justify-center flex-shrink-0 ${
                    st.done ? 'bg-ok text-white' : !st.unlocked ? 'bg-surface-3 text-fg-muted' : isCurrent || active ? 'bg-primary text-primary-fg' : 'bg-surface-active text-fg-3'}`}>
                    {st.done ? <Check className="w-3.5 h-3.5" aria-hidden /> : !st.unlocked ? <Lock className="w-3 h-3" aria-hidden /> : i + 1}
                  </span>
                  <span className="min-w-0">
                    <span className={`block text-sm font-medium truncate ${st.unlocked ? 'text-fg' : 'text-fg-muted'}`}>
                      {st.label}{st.optional && <span className="ml-1.5 text-[10px] font-normal text-fg-muted">optional</span>}
                    </span>
                    <span className={`block text-[11px] truncate ${st.done ? 'text-fg-3' : st.unlocked ? (isCurrent ? 'text-warn-text' : 'text-fg-3') : 'text-fg-muted'}`}>{st.summary}</span>
                  </span>
                </button>
              </li>
            )
          })}
        </ol>
      </nav>

      {!canManage && (
        <div className="bg-info-bg border border-info-border rounded-lg px-4 py-3 mb-6 text-xs text-info-text flex items-start gap-2">
          <Lock className="w-3.5 h-3.5 mt-0.5 flex-shrink-0" aria-hidden />
          <span>
            <span className="font-medium">Read-only.</span> This space is managed by its owner{space.owner ? ` (${space.owner})` : ''}.
            You can view and use it but not change it.
          </span>
        </div>
      )}
      {/* ── Overview: the read-only landing page of a set-up space. Outside the fieldset
             on purpose: its only controls are "Open" links into the stages, which a
             read-only viewer may follow (they change nothing), and every fact on it is
             the same flag or count the stages themselves show, never a second source. ── */}
      {activeTab === SPACE_OVERVIEW_SECTION.id && (() => {
        const open = (id, label) => (
          <button onClick={() => setActiveTab(id)}
            className="inline-flex items-center gap-1 text-xs font-medium text-primary hover:underline whitespace-nowrap">
            {label} <ArrowRight className="w-3.5 h-3.5" aria-hidden />
          </button>
        )
        const failedDbs = configuredDbs.filter(d => d.status === 'config_failed')
        const deleteFailedDbs = configuredDbs.filter(d => d.status === 'delete_failed')
        const busyDbs = configuredDbs.filter(d => d.status === 'configuring' || d.status === 'deleting')
        const unreachable = (coverage?.blocked || 0) + (coverage?.pending || 0)
        // What needs an admin's hand, worst first: each line names the stage to open.
        const attention = [
          !accessConfigured && { tone: 'warn', text: 'Access is not validated: the agent cannot reach any account yet.', stage: 'accounts', label: 'Open Accounts & regions' },
          hasPendingAccounts && { tone: 'warn', text: `${pendingSecondary.length} account${pendingSecondary.length > 1 ? 's' : ''} pending validation.`, stage: 'accounts', label: 'Open Accounts & regions' },
          regionsChanged && accessConfigured && { tone: 'warn', text: 'Regions changed: validate access again.', stage: 'accounts', label: 'Open Accounts & regions' },
          failedDbs.length > 0 && { tone: 'err', text: `${failedDbs.length} database${failedDbs.length > 1 ? 's' : ''} failed to onboard: ${failedDbs.map(d => d.display_name).join(', ')}.`, stage: 'databases', label: 'Open Databases' },
          deleteFailedDbs.length > 0 && { tone: 'err', text: `${deleteFailedDbs.length} removal${deleteFailedDbs.length > 1 ? 's' : ''} did not finish: ${deleteFailedDbs.map(d => d.display_name).join(', ')}.`, stage: 'databases', label: 'Open Databases' },
          unreachable > 0 && { tone: 'warn', text: `${unreachable} database${unreachable > 1 ? 's are' : ' is'} not reachable over the network path yet.`, stage: 'topology', label: 'Open Topology' },
          (topology?.warnings || []).length > 0 && { tone: 'warn', text: `${topology.warnings.length} topology warning${topology.warnings.length > 1 ? 's' : ''}.`, stage: 'topology', label: 'Open Topology' },
          accessDone && !onboardDone && { tone: 'warn', text: discoveredDbs.length ? 'No database is onboarded into this space yet.' : 'No database is onboarded yet: run discovery, then onboard with a read-only secret.', stage: 'databases', label: 'Open Databases' },
          accessDone && onboardDone && operatorCount === 0 && { tone: 'warn', text: 'Nobody can open this space in the operator app yet.', stage: 'operators', label: 'Open Operators' },
          onboardDone && !topology?.generated_at && { tone: 'info', text: 'The topology map has not been generated yet.', stage: 'topology', label: 'Open Topology' },
          busyDbs.length > 0 && { tone: 'info', text: `${busyDbs.length} database${busyDbs.length > 1 ? 's' : ''} still being set up or removed: ${busyDbs.map(d => d.display_name).join(', ')}.`, stage: 'databases', label: 'Open Databases' },
          discoveredDbs.length > 0 && { tone: 'info', text: `${discoveredDbs.length} database${discoveredDbs.length > 1 ? 's' : ''} found by discovery but not onboarded into this space.`, stage: 'databases', label: 'Open Databases' },
        ].filter(Boolean)
        const toneCls = { err: 'bg-err-bg border-err-border text-err-text', warn: 'bg-warn-bg border-warn-border text-warn-text', info: 'bg-info-bg border-info-border text-info-text' }
        const accountCount = new Set([space.primary_account_id, ...secondaryIds].filter(Boolean)).size
        return (
          <div className="space-y-6">
            {/* Needs attention, or the all-clear */}
            <div className="bg-surface rounded-lg border border-line p-6">
              <div className="flex items-center justify-between mb-3">
                <h2 className="text-sm font-semibold text-fg">Needs attention</h2>
                <span className="text-xs text-fg-3">{setupComplete ? 'Setup complete' : `${setupDone} of ${stages.length} stages done`}</span>
              </div>
              {attention.length === 0 ? (
                <div className="flex items-center gap-2 text-sm text-ok-text bg-ok-bg border border-ok-border rounded-lg px-4 py-3">
                  <Check className="w-4 h-4 flex-shrink-0" aria-hidden />
                  Nothing needs attention. {APP_NAME} can discover and monitor the databases in this Agent Space.
                </div>
              ) : (
                <ul className="space-y-2">
                  {attention.map((a, i) => (
                    <li key={i} className={`flex items-start justify-between gap-4 rounded-lg border px-4 py-2.5 text-xs ${toneCls[a.tone]}`}>
                      <span className="flex items-start gap-2 min-w-0">
                        <AlertTriangle className="w-3.5 h-3.5 mt-0.5 flex-shrink-0" aria-hidden />
                        <span className="break-words">{a.text}</span>
                      </span>
                      {open(a.stage, a.label)}
                    </li>
                  ))}
                </ul>
              )}
            </div>

            {/* One card per stage: its state, its one-line summary, a link into it */}
            <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
              {stages.map(st => (
                <div key={st.id} className="bg-surface rounded-lg border border-line p-4 flex flex-col gap-2">
                  <div className="flex items-start justify-between gap-3">
                    <div className="flex items-center gap-2 min-w-0">
                      <span className={`w-5 h-5 rounded-full flex items-center justify-center flex-shrink-0 ${st.done ? 'bg-ok text-white' : st.unlocked ? 'bg-warn text-warn-inverse' : 'bg-surface-3 text-fg-muted'}`}>
                        {st.done ? <Check className="w-3 h-3" aria-hidden /> : st.unlocked ? <AlertTriangle className="w-3 h-3" aria-hidden /> : <Lock className="w-2.5 h-2.5" aria-hidden />}
                      </span>
                      <h3 className="text-sm font-semibold text-fg truncate">{st.label}{st.optional && <span className="ml-1.5 text-[10px] font-normal text-fg-muted">optional</span>}</h3>
                    </div>
                    {st.unlocked && open(st.id, 'Open')}
                  </div>
                  <p className="text-xs text-fg-3 break-words">{st.summary}</p>
                  {st.id === 'accounts' && (
                    <p className="text-[11px] text-fg-muted">{accountCount} account{accountCount === 1 ? '' : 's'} · {(space.regions || []).length} region{(space.regions || []).length === 1 ? '' : 's'}{(space.regions || []).length ? `: ${(space.regions || []).join(', ')}` : ''}</p>
                  )}
                  {st.id === 'databases' && (
                    <p className="text-[11px] text-fg-muted">
                      {dbCounts.configured} ready{dbCounts.configuring ? ` · ${dbCounts.configuring} configuring` : ''}{dbCounts.config_failed ? ` · ${dbCounts.config_failed} failed` : ''}{dbCounts.deleting ? ` · ${dbCounts.deleting} removing` : ''}{dbCounts.delete_failed ? ` · ${dbCounts.delete_failed} removal failed` : ''}
                    </p>
                  )}
                  {st.id === 'topology' && coverage && (
                    <p className="text-[11px] text-fg-muted">{coverage.reachable ?? 0} reachable{coverage.pending ? ` · ${coverage.pending} pending` : ''}{coverage.blocked ? ` · ${coverage.blocked} blocked` : ''}{coverage.unknown ? ` · ${coverage.unknown} unknown` : ''} · metadata for {coverage.refreshed ?? 0} of {coverage.databases ?? allDbs.length}</p>
                  )}
                </div>
              ))}
            </div>

            {/* The space's databases, read-only; the Databases stage holds every action */}
            <div className="bg-surface rounded-lg border border-line p-6">
              <div className="flex items-center justify-between mb-3">
                <h2 className="text-sm font-semibold text-fg">Databases in this space ({allDbs.length})</h2>
                {open('databases', 'Open Databases')}
              </div>
              {allDbs.length === 0 ? (
                <p className="text-xs text-fg-3">No databases onboarded yet.</p>
              ) : (
                <div className="border border-line rounded-lg overflow-hidden">
                  <table className="w-full">
                    <thead className="bg-surface-2">
                      <tr>
                        <th className="px-3 py-2 text-left text-[10px] font-semibold text-fg-3 uppercase">Name</th>
                        <th className="px-3 py-2 text-left text-[10px] font-semibold text-fg-3 uppercase">Engine</th>
                        <th className="px-3 py-2 text-left text-[10px] font-semibold text-fg-3 uppercase">Region</th>
                        <th className="px-3 py-2 text-left text-[10px] font-semibold text-fg-3 uppercase">Status</th>
                        <th className="px-3 py-2 text-left text-[10px] font-semibold text-fg-3 uppercase">Monitoring</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-line-2">
                      {allDbs.map(db => {
                        const badge = STATUS_BADGE[db.status] || { label: db.status || 'Unknown', cls: 'bg-surface-3 text-fg-2 border-line' }
                        const t1 = db.tier1_enabled !== false, t2 = db.tier2_enabled !== false
                        return (
                          <tr key={db.instance_id}>
                            <td className="px-3 py-2 text-xs font-medium text-fg">
                              {db.display_name}
                              {memberCountLabel(db.cluster_member_roles) && <span className="block text-[10px] font-normal text-fg-muted">{memberCountLabel(db.cluster_member_roles)}</span>}
                            </td>
                            <td className="px-3 py-2 text-xs text-fg-2">{engineLabel(db.engine)}</td>
                            <td className="px-3 py-2 text-xs text-fg-3">{db.region}</td>
                            <td className="px-3 py-2"><span className={`px-2 py-0.5 rounded-full text-[10px] font-medium border whitespace-nowrap ${badge.cls}`}>{badge.label}</span></td>
                            <td className="px-3 py-2 text-[11px] text-fg-3 whitespace-nowrap">Tier 1 {t1 ? 'on' : 'off'} · Tier 2 {t2 ? 'on' : 'off'}</td>
                          </tr>
                        )
                      })}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          </div>
        )
      })()}

      <fieldset disabled={!canManage} className="min-w-0 border-0 p-0 m-0" aria-label="Space stages">
      {/* ── Stage 3: Agent capabilities (Knowledge | Skills | Model) ── */}
      {activeTab === 'capabilities' && (
        <div className="space-y-6">
          <div className="bg-info-bg border border-info-border rounded-lg px-4 py-3 text-xs text-info-text">
            <span className="font-medium">The defaults already work.</span> Every space inherits the account-level knowledge, skills and model. Change something here only if this space needs its own runbooks, documents or model choice; otherwise continue to Operators.
          </div>
          <AgentCapabilities api={api} layer="space" space={space} />
          <StageNav />
        </div>
      )}

      {/* ── Stage 1: Accounts & regions ── */}
      {activeTab === 'accounts' && (
        <div className="space-y-6">
          <div className="bg-surface rounded-lg border border-line p-6">
            <h2 className="text-sm font-semibold text-fg mb-1">Cloud sources</h2>
            <p className="text-xs text-fg-3 mb-4">The primary account is where {APP_NAME} runs. Add secondary accounts to let it reach databases elsewhere; they are validated in the next section.</p>
            <h3 className="text-xs font-semibold text-fg-2 mb-2">Primary source — required</h3>
            <div className="border border-line rounded-lg overflow-hidden mb-6">
              <table className="w-full">
                <thead className="bg-surface-2">
                  <tr>
                    <th className="px-3 py-2 text-left text-[10px] font-semibold text-fg-3 uppercase">Account ID</th>
                    <th className="px-3 py-2 text-left text-[10px] font-semibold text-fg-3 uppercase">Account Type</th>
                    <th className="px-3 py-2 text-left text-[10px] font-semibold text-fg-3 uppercase">Status</th>
                    <th className="px-3 py-2 text-left text-[10px] font-semibold text-fg-3 uppercase">Last Updated</th>
                  </tr>
                </thead>
                <tbody>
                  <tr>
                    <td className="px-3 py-2 text-sm font-mono text-fg-2">{space.primary_account_id || '—'}</td>
                    <td className="px-3 py-2 text-sm text-fg-2">Primary AWS Account</td>
                    <td className="px-3 py-2"><span className="text-xs text-ok font-medium">✓ Valid</span></td>
                    <td className="px-3 py-2 text-xs text-fg-3">{space.updated_at ? new Date(space.updated_at).toLocaleString() : '—'}</td>
                  </tr>
                </tbody>
              </table>
            </div>

            <div className="flex items-center justify-between mb-2">
              <h3 className="text-xs font-semibold text-fg-2">
                Secondary sources ({(space.secondary_account_ids || []).length})
              </h3>
              <button onClick={openAddAccount}
                className="px-3 py-1.5 text-xs font-medium text-primary border border-primary rounded-lg hover:bg-primary-subtle transition-colors">
                Add
              </button>
            </div>
            {(space.secondary_account_ids || []).length === 0 ? (
              <div className="border border-line rounded-lg p-4 text-center">
                <p className="text-xs text-fg-muted">No secondary accounts configured.</p>
                <button onClick={openAddAccount} className="mt-2 text-xs text-primary hover:underline">Add source</button>
              </div>
            ) : (
              <div className="border border-line rounded-lg overflow-hidden">
                <table className="w-full">
                  <thead className="bg-surface-2">
                    <tr>
                      <th className="px-3 py-2 text-left text-[10px] font-semibold text-fg-3 uppercase">Account ID</th>
                      <th className="px-3 py-2 text-left text-[10px] font-semibold text-fg-3 uppercase">Role</th>
                      <th className="px-3 py-2 text-left text-[10px] font-semibold text-fg-3 uppercase">Status</th>
                      <th className="px-3 py-2 w-8"></th>
                    </tr>
                  </thead>
                  <tbody>
                    {(space.secondary_account_ids || []).map(id => (
                      <tr key={id}>
                        <td className="px-3 py-2 text-sm font-mono text-fg-2">{id}</td>
                        <td className="px-3 py-2 text-xs font-mono">
                          {editingRole === id ? (
                            <input
                              autoFocus
                              value={editRoleValue}
                              onChange={e => setEditRoleValue(e.target.value)}
                              onBlur={async () => {
                                if (editRoleValue.trim() && editRoleValue.trim() !== (space.secondary_role_names?.[id] || '')) {
                                  await api.put(`/api/spaces/${space.space_id}`, {
                                    secondary_role_names: { ...space.secondary_role_names, [id]: editRoleValue.trim() },
                                  })
                                  onUpdate(await api.get(`/api/spaces/${space.space_id}`))
                                }
                                setEditingRole(null)
                              }}
                              onKeyDown={e => { if (e.key === 'Enter') e.target.blur(); if (e.key === 'Escape') setEditingRole(null) }}
                              className="w-full px-1.5 py-0.5 text-xs border border-primary rounded focus:outline-none focus:ring-1 focus:ring-primary font-mono"
                            />
                          ) : (
                            <div className="flex items-center gap-1.5">
                              <span className="text-fg-3">{space.secondary_role_names?.[id] || '—'}</span>
                              <button
                                onClick={() => { setEditingRole(id); setEditRoleValue(space.secondary_role_names?.[id] || '') }}
                                className="p-0.5 rounded text-fg-2 hover:text-primary hover:bg-primary-subtle transition-all"
                                title="Edit role name">
                                <svg className="w-3 h-3" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24">
                                  <path strokeLinecap="round" strokeLinejoin="round" d="M16.862 4.487l1.687-1.688a1.875 1.875 0 112.652 2.652L10.582 16.07a4.5 4.5 0 01-1.897 1.13L6 18l.8-2.685a4.5 4.5 0 011.13-1.897l8.932-8.931z" />
                                </svg>
                              </button>
                            </div>
                          )}
                        </td>
                        <td className="px-3 py-2 text-xs">
                          {space.account_statuses?.[id] === 'valid'
                            ? <span className="text-ok font-medium">✓ Valid</span>
                            : space.account_statuses?.[id] === 'failed'
                              ? <span className="text-err font-medium">✕ Failed</span>
                              : <span className="text-fg-3">Pending validation</span>
                          }
                        </td>
                        <td className="px-3 py-2">
                          <button onClick={async () => {
                            if (!confirm(`Remove account ${id}?`)) return
                            const updated = await api.put(`/api/spaces/${space.space_id}`, {
                              secondary_account_ids: (space.secondary_account_ids || []).filter(a => a !== id),
                            })
                            onUpdate(await api.get(`/api/spaces/${space.space_id}`))
                          }} className="text-fg-muted hover:text-err transition-colors" title="Remove">✕</button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          <div className="bg-surface rounded-lg border border-line p-6">
            {/* Regions and access are one decision: pick where to look, then prove the
                accounts can be reached there. Validate stays disabled until a region is chosen. */}
            <h2 className="text-sm font-semibold text-fg mb-1">Regions & access</h2>
            <p className="text-xs text-fg-3 mb-3">For each account, pick the regions it has databases in; discovery scans exactly those. Leave an account empty to skip it — the deployment account can stay empty if it has no databases. Then validate access: this verifies every account's role and sets up Resource Explorer in every region in use.</p>
            {/* One row per account, the primary included. A row shows its regions as a
                summary and opens a picker on Edit; "Apply to all accounts" copies one
                account's regions everywhere. A fleet of twenty accounts stays one short
                list, and there is no second concept (a default) to learn. */}
            <div className="space-y-2 mb-4" role="list" aria-label="Regions per account">
              {allAccountIds().map(id => {
                const rs = regionsFor(id)
                const open = editingRegions.has(id)
                const isPrimary = id === space.primary_account_id
                return (
                  <div key={id} role="listitem" className="rounded-lg border border-line p-3">
                    <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                      <span className="text-sm font-mono text-fg-2">{id}</span>
                      {isPrimary && <span className="text-[10px] px-1.5 py-0.5 rounded bg-surface-3 text-fg-3">primary</span>}
                      <span className="text-xs min-w-0 truncate flex-1">
                        {rs.length
                          ? <span className="text-fg-3">{rs.length} region{rs.length > 1 ? 's' : ''}: <span className="text-fg-2">{[...rs].sort().join(', ')}</span></span>
                          : scansNowhere
                            ? <span className="text-warn-text">Pick at least one region to scan</span>
                            : <span className="text-fg-muted">No regions — not scanned</span>}
                      </span>
                      <div className="flex items-center gap-1 ml-auto">
                        {allAccountIds().length > 1 && rs.length > 0 && (
                          <button type="button" onClick={() => applyRegionsToAll(id)}
                            className="px-2.5 py-1 text-[11px] rounded-lg border border-line text-fg-3 hover:bg-surface-3 transition-colors"
                            title={`Give every account the same regions as ${id}`}>
                            Apply to all accounts
                          </button>
                        )}
                        <button type="button" onClick={() => toggleEditingRegions(id)} aria-expanded={open} aria-controls={`regions-${id}`}
                          className={`px-2.5 py-1 text-[11px] rounded-lg border transition-colors ${open ? 'bg-primary-subtle text-primary border-primary-line font-medium' : 'border-line text-fg-3 hover:bg-surface-3'}`}>
                          {open ? 'Done' : 'Edit'}
                        </button>
                      </div>
                    </div>
                    {open && (
                      <div id={`regions-${id}`} className="mt-3 space-y-2">
                        {REGION_GROUPS.map(g => {
                          const regions = AWS_REGIONS.filter(g.test)
                          if (!regions.length) return null
                          return (
                            <div key={g.label} className="flex flex-wrap items-center gap-1.5">
                              <span className="text-[10px] font-semibold uppercase tracking-wider text-fg-muted w-32 flex-shrink-0">{g.label}</span>
                              {regions.map(r => {
                                const on = rs.includes(r)
                                return (
                                  <button key={r} type="button" onClick={() => toggleAccountRegion(id, r)} aria-pressed={on}
                                    className={`px-2.5 py-1 text-[11px] rounded-lg border transition-colors ${
                                      on ? 'bg-primary text-primary-fg border-primary' : 'bg-surface text-fg-2 border-line hover:border-primary-line'}`}>{r}</button>
                                )
                              })}
                            </div>
                          )
                        })}
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
            <p className="text-xs text-fg-muted mb-3">
              {scannedRegions.length
                ? <>Discovery will scan {scannedRegions.length} region{scannedRegions.length > 1 ? 's' : ''} in total: {scannedRegions.join(', ')}.</>
                : 'Nothing will be scanned until an account has a region.'}
            </p>
            <div className="flex flex-col gap-2 pt-3 border-t border-line-2">
              <div className="flex items-center gap-3">
                {needsReconfigure ? (
                  <button onClick={handleConfigureAccess}
                    disabled={configuringAccess || scansNowhere}
                    className="inline-flex items-center gap-1.5 px-4 py-2 text-sm font-medium bg-primary text-primary-fg rounded-lg hover:bg-primary-hover transition-colors disabled:opacity-50 disabled:cursor-not-allowed">
                    {configuringAccess ? 'Validating...' : (regionsChanged || hasPendingAccounts) && accessConfigured
                      ? <><RefreshCw className="w-4 h-4" aria-hidden /> Validate access again</>
                      : <><Settings2 className="w-4 h-4" aria-hidden /> Validate access</>}
                  </button>
                ) : (
                  <span className="inline-flex items-center gap-1.5 text-sm text-ok-text font-medium"><Check className="w-4 h-4" aria-hidden /> All accounts validated in {scannedRegions.length} region{scannedRegions.length > 1 ? 's' : ''}.</span>
                )}
                {scansNowhere && <span className="text-xs text-warn-text">Pick regions for at least one account to enable validation.</span>}
                {!scansNowhere && hasPendingAccounts && <span className="text-xs text-warn-text">New accounts need validation.</span>}
                {!scansNowhere && regionsChanged && accessConfigured && <span className="text-xs text-warn-text">Regions changed — validate again.</span>}
              </div>
              {accessError && (
                <div className="bg-err-bg border border-err-border rounded-lg p-3 text-xs text-err-text whitespace-pre-line">{accessError}</div>
              )}
            </div>
          </div>

          {/* Danger zone lives with the space's own settings. */}
          <div className="rounded-lg border border-err-border bg-surface p-6">
            <div className="flex flex-wrap items-center gap-4">
              <div className="flex-1 min-w-[240px]">
                <h2 className="text-sm font-semibold text-fg mb-1">Delete this Agent Space</h2>
                <p className="text-xs text-fg-3">Removes the space and its operator access. Databases shared with other spaces stay with them. This cannot be undone.</p>
              </div>
              <button onClick={handleDelete} disabled={deleting}
                className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm text-err border border-err-border rounded-lg hover:bg-err-hover transition-colors disabled:opacity-50">
                <Trash2 className="w-4 h-4" aria-hidden />{deleting ? 'Deleting...' : 'Delete Agent Space'}
              </button>
            </div>
          </div>
          <StageNav />
        </div>
      )}

      {/* ── Stage 2: Databases (discover, onboard, and the onboarded table) ── */}
      {activeTab === 'databases' && (
        <div className="space-y-6">
          <div className="bg-surface rounded-lg border border-line p-6">
            {/* Discovery only FINDS databases; it grants nothing. Each one this space
                should include is onboarded here with a read-only secret, which is what
                makes it this space's database (and visible to its operators). */}
            <h2 className="text-sm font-semibold text-fg mb-1">Discover & onboard</h2>
            <p className="text-xs text-fg-3 mb-3">Scans each account in its regions for PostgreSQL databases. Onboard the ones this space should include by supplying a read-only user's secret — that is what makes a database part of this space.</p>

            {/* Account scope — only worth showing when there is more than one account. */}
            {spaceAccountIds.length > 1 && (
              <div className="mb-3">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="text-[10px] font-semibold uppercase tracking-wider text-fg-muted">Scan</span>
                  <button type="button" onClick={() => setDiscoverAccounts(new Set())} aria-pressed={discoverAccounts.size === 0}
                    className={`px-2.5 py-1 text-[11px] rounded-full border transition-colors ${discoverAccounts.size === 0 ? 'bg-primary-subtle text-primary border-primary-line font-medium' : 'text-fg-3 border-line hover:bg-surface-3'}`}>
                    All accounts ({spaceAccountIds.length})
                  </button>
                  {spaceAccountIds.map(id => (
                    <button key={id} type="button" onClick={() => toggleDiscoverAccount(id)} aria-pressed={discoverAccounts.has(id)}
                      title={id === space.primary_account_id ? 'Primary account' : 'Secondary account'}
                      className={`px-2.5 py-1 text-[11px] font-mono rounded-full border transition-colors ${discoverAccounts.has(id) ? 'bg-primary-subtle text-primary border-primary-line font-medium' : 'text-fg-3 border-line hover:bg-surface-3'}`}>
                      {id}{id === space.primary_account_id ? ' · primary' : ''}
                    </button>
                  ))}
                </div>
                <p className="text-[10px] text-fg-muted mt-1">Pick specific accounts to scan only those; databases in accounts you skip are left as they are.</p>
              </div>
            )}

            <div className="flex items-center gap-3 flex-wrap">
              <button onClick={handleDiscover} disabled={discovering || !accessConfigured}
                className="inline-flex items-center gap-1.5 px-4 py-2 text-sm font-medium bg-primary text-primary-fg rounded-lg hover:bg-primary-hover transition-colors disabled:opacity-50 disabled:cursor-not-allowed">
                {discovering ? 'Discovering...' : <><Search className="w-4 h-4" aria-hidden /> {discoveredDbs.length || configuredDbs.length ? 'Run discovery again' : 'Run discovery'}{spaceAccountIds.length > 1 ? ` · ${discoverScopeLabel}` : ''}</>}
              </button>
              {discoveryResult && (
                <span className="text-xs text-fg-3">
                  {discoveryResult.total_discovered} found · {discoveryResult.new_added} new · {discoveryResult.proposed_existing || 0} proposed to this space
                  {Array.isArray(discoveryResult.accounts_scanned) && spaceAccountIds.length > 1 && (
                    <> · scanned {discoveryResult.accounts_scanned.length} of {spaceAccountIds.length} account{spaceAccountIds.length > 1 ? 's' : ''}</>
                  )}
                </span>
              )}
            </div>
            {discoveryResult?.scan_log && (
              <details className="mt-3">
                <summary className="text-[11px] text-fg-muted cursor-pointer hover:text-fg-2">Scan log</summary>
                <pre className="mt-1 bg-surface-2 rounded p-2 text-[10px] text-fg-3 font-mono overflow-x-auto max-h-40">{discoveryResult.scan_log.join('\n')}</pre>
              </details>
            )}

            {discoveredDbs.length > 0 && (
              <div className="mt-4">
                <div className="mb-2">
                  <DbListToolbar c={foundCtl} title={`Found, not yet onboarded (${discoveredDbs.length})`} placeholder="Search found databases..." />
                </div>
                <div className="border border-line rounded-lg overflow-hidden">
                  <table className="w-full">
                    <thead className="bg-surface-2">
                      <tr>
                        <th className="px-3 py-2 text-left text-[10px] font-semibold text-fg-3 uppercase">Name</th>
                        <th className="px-3 py-2 text-left text-[10px] font-semibold text-fg-3 uppercase">Account</th>
                        <th className="px-3 py-2 text-left text-[10px] font-semibold text-fg-3 uppercase">Engine</th>
                        <th className="px-3 py-2 text-left text-[10px] font-semibold text-fg-3 uppercase">Region</th>
                        <th className="px-3 py-2 text-left text-[10px] font-semibold text-fg-3 uppercase">Status</th>
                        <th className="px-3 py-2 text-right text-[10px] font-semibold text-fg-3 uppercase">Actions</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-line-2">
                      {foundPage.rows.length === 0 && (
                        <tr><td colSpan={6} className="px-3 py-4 text-center text-xs text-fg-muted">No databases match the current search and filters.</td></tr>
                      )}
                      {foundPage.rows.map(({ row: db, depth, parentName }) => (
                        <React.Fragment key={db.instance_id}>
                        <tr>
                          <td className="px-3 py-2 text-sm font-medium text-fg" style={depth ? { paddingLeft: `${0.75 + depth * 1.25}rem` } : undefined}>
                            {depth > 0 && <span className="text-fg-muted mr-1" aria-hidden>↳</span>}{db.display_name}
                            <ReplicaLabel name={parentName} />
                          </td>
                          <td className="px-3 py-2 text-xs font-mono text-fg-3">{db.account_id}</td>
                          <td className="px-3 py-2 text-xs text-fg-3">{engineLabel(db.engine)}</td>
                          <td className="px-3 py-2 text-xs text-fg-3">{db.region}</td>
                          <td className="px-3 py-2">
                            {(db.status === 'configuring' || db.status === 'config_failed' || REMOVING.has(db.status))
                              ? <span className={`inline-flex items-center px-2 py-0.5 rounded text-[10px] font-medium border ${STATUS_BADGE[db.status].cls}`}
                                  title={db.status === 'configuring' ? 'Building the network path and verifying the login; it joins this space once that succeeds'
                                    : db.status === 'deleting' ? 'Removing the network path this onboard built'
                                    : db.status === 'delete_failed' ? (db.config_error || 'Part of the network path is still in place; Retry finishes the removal')
                                    : 'The network path or the login failed; onboard again to retry'}>
                                  {STATUS_BADGE[db.status].label}
                                </span>
                            : db.onboarded_elsewhere
                              ? <span className="inline-flex items-center px-2 py-0.5 rounded text-[10px] font-medium border bg-info-bg text-info-text border-info-border"
                                  title="Another space already onboarded this database. Onboarding here verifies your secret and joins it; the network path and monitoring are shared.">
                                  Runs in {db.shared_with_count} other space{db.shared_with_count > 1 ? 's' : ''}
                                </span>
                              : <span className="inline-flex items-center px-2 py-0.5 rounded text-[10px] font-medium border bg-warn-bg text-warn-text border-warn-border">New</span>}
                          </td>
                          <td className="px-3 py-2">
                            <div className="flex items-center justify-end gap-1.5">
                              <button onClick={() => !REMOVING.has(db.status) && openConfigDbWizard(db)}
                                disabled={REMOVING.has(db.status)} aria-disabled={REMOVING.has(db.status)}
                                className={`inline-flex items-center gap-1 px-2.5 py-1 text-[11px] font-medium rounded-lg transition-colors disabled:opacity-40 disabled:cursor-not-allowed ${
                                  db.suggested_secret_arn ? 'bg-primary text-primary-fg hover:bg-primary-hover' : 'text-primary border border-primary-line hover:bg-primary-subtle'}`}
                                title={db.suggested_secret_arn
                                  ? `Your space ${db.suggested_secret_from} already holds a credential for this database: Test it and Add`
                                  : db.onboarded_elsewhere ? 'Onboard with a read-only secret this space can read' : 'Onboard this database with a read-only secret'}>
                                <Settings2 className="w-3 h-3" aria-hidden /> {db.onboarded_elsewhere ? 'Add to this space' : 'Onboard'}
                              </button>
                              {db.status === 'delete_failed' ? (
                                <button onClick={() => handleRetryRemoval(db, true)}
                                  className="inline-flex items-center gap-1 px-2.5 py-1 text-[11px] font-medium text-err border border-err-border rounded-lg hover:bg-err-hover transition-colors"
                                  title="Finish removing the network path this onboard built" aria-label={`Retry removing ${db.display_name}`}>
                                  <RefreshCw className="w-3 h-3" aria-hidden /> Retry
                                </button>
                              ) : (
                              <button onClick={() => handleDismissCandidate(db.instance_id, db.display_name)}
                                disabled={db.status === 'deleting'}
                                className="p-1.5 rounded-md text-fg-muted hover:text-err hover:bg-err-hover disabled:opacity-40 disabled:cursor-not-allowed transition-all"
                                title={db.status === 'deleting' ? 'Being removed' : 'Not for this space — hide until the next discovery'} aria-label={`Dismiss ${db.display_name}`}>
                                <Trash2 className="w-4 h-4" aria-hidden />
                              </button>
                              )}
                            </div>
                          </td>
                        </tr>
                        <FailureDetail db={db} colSpan={6} onClaimRoute={handleClaimRoute} />
                        </React.Fragment>
                      ))}
                    </tbody>
                  </table>
                  <DbListPager c={foundCtl} count={foundPage.totalGroups} totalPages={foundPage.totalPages} page={foundPage.page} />
                </div>
              </div>
            )}

            {/* Found but not onboardable (an Aurora Global Database member, or a cluster
                whose membership could not be checked): never candidates, so listed here
                with the reason and no action. */}
            {notSupported.length > 0 && (
              <details className="mt-4 group">
                <summary className="text-xs font-semibold text-fg-2 cursor-pointer hover:text-fg select-none">
                  Not supported ({notSupported.length})
                </summary>
                <div className="mt-2 border border-line rounded-lg overflow-hidden">
                  <table className="w-full">
                    <caption className="sr-only">Databases discovery found that cannot be onboarded</caption>
                    <thead className="bg-surface-2">
                      <tr>
                        <th scope="col" className="px-3 py-2 text-left text-[10px] font-semibold text-fg-3 uppercase">Name</th>
                        <th scope="col" className="px-3 py-2 text-left text-[10px] font-semibold text-fg-3 uppercase">Region</th>
                        <th scope="col" className="px-3 py-2 text-left text-[10px] font-semibold text-fg-3 uppercase">Reason</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-line-2">
                      {notSupported.map(d => (
                        <tr key={`${d.account}/${d.region}/${d.name}`}>
                          <td className="px-3 py-2 text-sm text-fg-2">{d.name}</td>
                          <td className="px-3 py-2 text-xs text-fg-3">{d.region}</td>
                          <td className="px-3 py-2 text-xs text-fg-3">{d.reason}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </details>
            )}
          </div>

          {/* Onboarded databases — one table, status as a column, per-status actions. */}
          <div className="bg-surface rounded-lg border border-line p-6">
            <div className="flex flex-wrap items-center gap-3 mb-4">
              <h2 className="text-sm font-semibold text-fg">Onboarded databases ({allDbs.length})</h2>
              <div className="flex items-center gap-1 flex-wrap" role="group" aria-label="Filter databases by status">
                {[['all', 'All'], ['configured', 'Ready'], ['configuring', 'Configuring'], ['config_failed', 'Failed'], ['deleting', 'Deleting'], ['delete_failed', 'Delete failed']]
                  .filter(([k]) => k === 'all' || dbCounts[k] > 0)
                  .map(([k, label]) => (
                    <button key={k} onClick={() => setDbFilter(k)} aria-pressed={dbFilter === k}
                      className={`px-2.5 py-1 text-[11px] rounded-full border transition-colors ${
                        dbFilter === k ? 'bg-primary-subtle text-primary border-primary-line font-medium' : 'text-fg-3 border-line hover:bg-surface-3'}`}>
                      {label} <span className="opacity-70">{dbCounts[k]}</span>
                    </button>
                  ))}
              </div>
              <div className="ml-auto flex items-center gap-2">
                {selectedMonDbs.size > 0 ? (
                  <>
                    <span className="text-[11px] text-primary font-medium">{selectedMonDbs.size} selected</span>
                    <button onClick={() => setBulkMonConfig({ tier1_frequency: '5min', tier2_frequency: 'daily', tier1_enabled: true, tier2_enabled: true })}
                      className="px-3 py-1.5 text-xs font-medium text-primary-fg bg-primary rounded-lg hover:bg-primary-hover transition-colors">
                      Apply monitoring
                    </button>
                    <button onClick={() => setSelectedMonDbs(new Set())}
                      className="px-3 py-1.5 text-xs text-fg-2 border border-line rounded-lg hover:bg-surface-3 transition-colors">Cancel</button>
                  </>
                ) : configuredDbs.length > 0 && (
                  <button onClick={() => setSelectedMonDbs(new Set(configuredDbs.map(d => d.instance_id)))}
                    className="px-3 py-1.5 text-xs text-fg-2 border border-line rounded-lg hover:bg-surface-3 transition-colors">
                    Configure monitoring
                  </button>
                )}
              </div>
            </div>

            {/* Search + engine/account/region filters (the status chips above are the
                status filter), shared with the operator app's Dashboard. */}
            {allDbs.length > 0 && (
              <div className="mb-3">
                <DbListToolbar c={onboardedCtl} placeholder="Search onboarded databases..." />
              </div>
            )}

            {/* Bulk monitoring config modal */}
            {bulkMonConfig && selectedMonDbs.size > 0 && (
              <div className="fixed inset-0 bg-black/30 z-50 flex items-center justify-center" onClick={() => setBulkMonConfig(null)}>
                <div className="bg-surface rounded-xl shadow-xl p-6 w-full max-w-sm mx-4" onClick={e => e.stopPropagation()}>
                  <h3 className="text-sm font-semibold text-fg mb-1">Configure Monitoring</h3>
                  <p className="text-xs text-fg-3 mb-4">Apply to {selectedMonDbs.size} selected database{selectedMonDbs.size > 1 ? 's' : ''}.</p>

                  <div className="space-y-4">
                    <div>
                      <div className="flex items-center justify-between mb-1.5">
                        <label className="text-xs font-medium text-fg-2">Tier 1 — Metrics Collection</label>
                        <button onClick={() => setBulkMonConfig(prev => ({ ...prev, tier1_enabled: !prev.tier1_enabled }))}
                          className={`text-[10px] px-2 py-0.5 rounded-full font-medium ${bulkMonConfig.tier1_enabled ? 'bg-ok-bg-2 text-ok-text' : 'bg-surface-3 text-fg-muted'}`}>
                          {bulkMonConfig.tier1_enabled ? 'ON' : 'OFF'}
                        </button>
                      </div>
                      {bulkMonConfig.tier1_enabled && (
                        <select value={bulkMonConfig.tier1_frequency} onChange={e => setBulkMonConfig(prev => ({ ...prev, tier1_frequency: e.target.value }))}
                          className="w-full text-xs border border-line rounded-lg px-3 py-2 bg-surface">
                          {TIER1_OPTIONS.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
                        </select>
                      )}
                    </div>

                    <div>
                      <div className="flex items-center justify-between mb-1.5">
                        <label className="text-xs font-medium text-fg-2">Tier 2 — Deep Scan</label>
                        <button onClick={() => setBulkMonConfig(prev => ({ ...prev, tier2_enabled: !prev.tier2_enabled }))}
                          className={`text-[10px] px-2 py-0.5 rounded-full font-medium ${bulkMonConfig.tier2_enabled ? 'bg-ok-bg-2 text-ok-text' : 'bg-surface-3 text-fg-muted'}`}>
                          {bulkMonConfig.tier2_enabled ? 'ON' : 'OFF'}
                        </button>
                      </div>
                      {bulkMonConfig.tier2_enabled && (
                        <select value={bulkMonConfig.tier2_frequency} onChange={e => setBulkMonConfig(prev => ({ ...prev, tier2_frequency: e.target.value }))}
                          className="w-full text-xs border border-line rounded-lg px-3 py-2 bg-surface">
                          {TIER2_OPTIONS.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
                        </select>
                      )}
                    </div>
                  </div>

                  <div className="flex justify-end gap-2 mt-6 pt-4 border-t border-line-2">
                    <button onClick={() => setBulkMonConfig(null)}
                      className="px-4 py-2 text-xs text-fg-2 border border-line rounded-lg hover:bg-surface-3 transition-colors">Cancel</button>
                    <button onClick={async () => {
                      // Apply per database through the cascade-aware endpoint: a space admin
                      // may edit a database's cadence only when this is its sole space with a
                      // single managing admin; a shared database is read-only to them and a
                      // super admin arbitrates. The per-database endpoint applies what the
                      // caller may change and names what it could not, so a space admin is not
                      // blanket-denied the way a super-admin-only bulk endpoint would be.
                      setApplyingBulk(true)
                      const refused = []
                      for (const id of selectedMonDbs) {
                        try { await api.put(`/api/admin/monitoring-config/${id}`, bulkMonConfig) }
                        catch (e) { refused.push(`${configuredDbs.find(d => d.instance_id === id)?.display_name || id}: ${e.detail || e.message}`) }
                      }
                      setBulkMonConfig(null)
                      setSelectedMonDbs(new Set())
                      await fetchDatabases()
                      setApplyingBulk(false)
                      if (refused.length) alert(`Not changed:\n${refused.join('\n')}`)
                    }} disabled={applyingBulk}
                      className="px-4 py-2 text-xs font-medium text-primary-fg bg-primary rounded-lg hover:bg-primary-hover disabled:opacity-50 transition-colors">
                      {applyingBulk ? 'Applying...' : 'Apply to Selected'}
                    </button>
                  </div>
                </div>
              </div>
            )}


            {allDbs.length === 0 ? (
              <div className="border border-line rounded-lg p-8 text-center">
                <p className="text-sm text-fg-2">No databases onboarded yet.</p>
                <p className="text-xs text-fg-muted mt-1">
                  {discoveredDbs.length
                    ? `${discoveredDbs.length} found above. Onboard the ones this space should include.`
                    : 'Run discovery above, then onboard each database with its read-only secret.'}
                </p>
              </div>
            ) : (
              <div className="border border-line rounded-lg overflow-hidden">
                <table className="w-full">
                  <thead className="bg-surface-2">
                    <tr>
                      {selectedMonDbs.size > 0 && (
                        <th className="px-2 py-2 w-8">
                          <input type="checkbox" aria-label="Select all configured databases"
                            checked={selectedMonDbs.size === configuredDbs.length && configuredDbs.length > 0}
                            onChange={e => setSelectedMonDbs(e.target.checked ? new Set(configuredDbs.map(d => d.instance_id)) : new Set())}
                            className="w-3 h-3" />
                        </th>
                      )}
                      <th className="px-3 py-2 text-left text-[10px] font-semibold text-fg-3 uppercase">Name</th>
                      <th className="px-3 py-2 text-left text-[10px] font-semibold text-fg-3 uppercase">Account</th>
                      <th className="px-3 py-2 text-left text-[10px] font-semibold text-fg-3 uppercase">Engine</th>
                      <th className="px-3 py-2 text-left text-[10px] font-semibold text-fg-3 uppercase">Region</th>
                      <th className="px-3 py-2 text-left text-[10px] font-semibold text-fg-3 uppercase">Endpoint</th>
                      <th className="px-3 py-2 text-left text-[10px] font-semibold text-fg-3 uppercase">Status</th>
                      <th className="px-3 py-2 text-left text-[10px] font-semibold text-fg-3 uppercase">Monitoring</th>
                      <th className="px-3 py-2 text-right text-[10px] font-semibold text-fg-3 uppercase">Actions</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-line-2">
                    {onboardedPage.rows.length === 0 && visibleDbs.length > 0 && (
                      <tr><td colSpan={9} className="px-3 py-4 text-center text-xs text-fg-muted">No databases match the current search and filters.</td></tr>
                    )}
                    {onboardedPage.rows.map(({ row: db, depth, parentName }) => {
                      const isDiscovered = db.status === 'discovered'
                      const badge = STATUS_BADGE[db.status] || { label: db.status || 'Unknown', cls: 'bg-surface-3 text-fg-2 border-line' }
                      const busy = configuringDbs.has(db.instance_id)
                      return (
                      <React.Fragment key={db.instance_id}>
                        <tr className="hover:bg-surface-2">
                          {selectedMonDbs.size > 0 && (
                            <td className="px-2 py-2">
                              {!isDiscovered && (
                                <input type="checkbox" aria-label={`Select ${db.display_name}`}
                                  checked={selectedMonDbs.has(db.instance_id)}
                                  onChange={e => setSelectedMonDbs(prev => {
                                    const n = new Set(prev)
                                    e.target.checked ? n.add(db.instance_id) : n.delete(db.instance_id)
                                    return n
                                  })}
                                  className="w-3 h-3" />
                              )}
                            </td>
                          )}
                          <td className="px-3 py-2 text-sm font-medium text-fg" style={depth ? { paddingLeft: `${0.75 + depth * 1.25}rem` } : undefined}>
                            {canRename && isDiscovered && editingName === db.instance_id ? (
                              <input autoFocus value={editNameValue} onChange={e => setEditNameValue(e.target.value)}
                                onBlur={() => handleRenameDb(db.instance_id)}
                                onKeyDown={e => { if (e.key === 'Enter') handleRenameDb(db.instance_id); if (e.key === 'Escape') setEditingName(null) }}
                                className="w-full px-1.5 py-0.5 text-sm border border-primary rounded focus:outline-none focus:ring-1 focus:ring-primary" />
                            ) : canRename && isDiscovered ? (
                              <span onClick={() => { setEditingName(db.instance_id); setEditNameValue(db.display_name) }}
                                className="cursor-pointer hover:text-primary hover:underline decoration-dashed underline-offset-2" title="Click to rename">
                                {db.display_name}
                              </span>
                            ) : <>{depth > 0 && <span className="text-fg-muted mr-1" aria-hidden>↳</span>}{db.display_name}</>}
                            <ReplicaLabel name={parentName} />
                            {memberCountLabel(db.cluster_member_roles) && (
                              <span className="block text-[10px] font-normal text-fg-muted">{memberCountLabel(db.cluster_member_roles)}</span>
                            )}
                          </td>
                          <td className="px-3 py-2 text-xs text-fg-3 font-mono">{db.account_id || '—'}</td>
                          <td className="px-3 py-2 text-xs text-fg-2">{engineLabel(db.engine)}</td>
                          <td className="px-3 py-2 text-xs text-fg-2">{db.region}</td>
                          <td className="px-3 py-2 text-[10px] text-fg-3 font-mono truncate max-w-[200px]" title={db.endpoint || ''}>{db.endpoint || '—'}</td>
                          <td className="px-3 py-2">
                            <span className={`px-2 py-0.5 rounded-full text-[10px] font-medium border whitespace-nowrap ${busy ? STATUS_BADGE.configuring.cls : badge.cls}`}
                              title={db.status === 'config_failed' ? (db.config_error || '') : ''}>
                              {busy ? STATUS_BADGE.configuring.label : badge.label}
                            </span>
                          </td>
                          <td className="px-3 py-2">
                            {isDiscovered ? <span className="text-[10px] text-fg-muted">—</span> : <MonitoringCell api={api} db={db} />}
                          </td>
                          <td className="px-3 py-2">
                            <div className="flex items-center justify-end gap-1.5">
                              {isDiscovered ? (
                                <>
                                  <button onClick={() => openConfigDbWizard(db)} disabled={busy}
                                    className="inline-flex items-center gap-1 px-2.5 py-1 text-[11px] font-medium text-primary border border-primary-line rounded-lg hover:bg-primary-subtle disabled:opacity-40 transition-colors"
                                    title="Configure this database">
                                    <Settings2 className="w-3 h-3" aria-hidden /> Configure
                                  </button>
                                  <button onClick={() => handleDeleteDb(db.instance_id, db.display_name)} disabled={deletingDbs.has(db.instance_id)}
                                    className="p-1.5 rounded-md text-fg-muted hover:text-err hover:bg-err-hover disabled:opacity-50 transition-all"
                                    title="Remove from this space" aria-label={`Remove ${db.display_name}`}>
                                    {deletingDbs.has(db.instance_id) ? <span className="text-[10px]">…</span> : <Trash2 className="w-4 h-4" aria-hidden />}
                                  </button>
                                </>
                              ) : (
                                <>
                                  {REMOVING.has(db.status) ? (db.status === 'delete_failed' ? (
                                    <button onClick={() => handleRetryRemoval(db, false)}
                                      className="inline-flex items-center gap-1 px-2.5 py-1 text-[11px] font-medium text-err border border-err-border rounded-lg hover:bg-err-hover transition-colors"
                                      title="Finish removing this database" aria-label={`Retry removing ${db.display_name}`}>
                                      <RefreshCw className="w-3 h-3" aria-hidden /> Retry
                                    </button>
                                  ) : <span className="text-[10px] text-fg-muted">Removing…</span>) : (<>
                                  <button
                                    onClick={async () => {
                                      setTestingConnection(db.instance_id)
                                      try {
                                        const r = await api.get(`/api/databases/${db.instance_id}/test-connection`)
                                        setConnectionResults(prev => ({ ...prev, [db.instance_id]: r }))
                                      } catch (e) {
                                        setConnectionResults(prev => ({ ...prev, [db.instance_id]: { error: e.detail || e.message } }))
                                      }
                                      setTestingConnection(null)
                                    }}
                                    disabled={testingConnection === db.instance_id}
                                    className="p-1.5 rounded-md text-fg-muted hover:text-primary hover:bg-primary-subtle disabled:opacity-50 transition-all"
                                    title="Test connection" aria-label={`Test connection to ${db.display_name}`}>
                                    {testingConnection === db.instance_id
                                      ? <RefreshCw className="w-4 h-4 animate-spin" aria-hidden />
                                      : <Zap className="w-4 h-4" aria-hidden />}
                                  </button>
                                  <button
                                    onClick={async () => {
                                      try {
                                        await api.post(`/api/spaces/${space.space_id}/databases/${db.instance_id}/configure`)
                                        await fetchDatabases()
                                      } catch (e) {
                                        setConfigLog(prev => [...prev, `Reconfigure ${db.display_name} failed: ${e.detail || e.message}`])
                                      }
                                    }}
                                    disabled={db.status === 'configuring'}
                                    className="p-1.5 rounded-md text-fg-muted hover:text-warn-mid hover:bg-warn-hover disabled:opacity-50 transition-all"
                                    title="Reconfigure network" aria-label={`Reconfigure ${db.display_name}`}>
                                    <RefreshCw className="w-4 h-4" aria-hidden />
                                  </button>
                                  <button onClick={() => handleDeleteDb(db.instance_id, db.display_name)} disabled={deletingDbs.has(db.instance_id)}
                                    className="p-1.5 rounded-md text-fg-muted hover:text-err hover:bg-err-hover disabled:opacity-50 transition-all"
                                    title={(db.member_count ?? db.member_space_ids?.length ?? 1) > 1 ? 'Remove from this space (other spaces keep it)' : 'Remove from this space'} aria-label={`Remove ${db.display_name} from this space`}>
                                    <Trash2 className="w-4 h-4" aria-hidden />
                                  </button>
                                  </>)}
                                </>
                              )}
                            </div>
                          </td>
                        </tr>
                        <FailureDetail db={db} colSpan={9} onClaimRoute={handleClaimRoute} />

                        {/* Cross-Region host routes: Tier 1 flagged drift (route_repair_needed);
                            the API task repairs it within about 5 to 10 minutes, or now with
                            Repair. A refusal keeps the flag with its reason. Owners and super
                            admins only (the field is in their projection alone). */}
                        {db.status === 'configured' && db.route_repair_needed && (
                          <tr>
                            <td colSpan={9} className={`px-3 py-2 text-[11px] ${db.route_repair_needed.refused ? 'bg-err-bg text-err-text' : 'bg-warn-bg text-warn-text'}`}>
                              <div className="flex items-center gap-3">
                                <span role="status" className="flex-1 min-w-0">
                                  {db.route_repair_needed.refused
                                    ? <><span className="font-medium">Route repair refused: </span>{db.route_repair_needed.refused}</>
                                    : <><span className="font-medium">Route repair needed</span>{db.route_repair_needed.member ? `: ${db.route_repair_needed.member}` : ''}
                                      {db.route_repair_needed.address ? ` (${db.route_repair_needed.address})` : ''}
                                      {db.route_repair_needed.detected_at ? `, detected ${new Date(db.route_repair_needed.detected_at).toLocaleString()}` : ''}.
                                      {' '}It is repaired automatically within about 5 to 10 minutes.</>}
                                </span>
                                <button onClick={() => handleRouteRepair(db)} disabled={repairingRoutes.has(db.instance_id)}
                                  className="inline-flex items-center gap-1 px-2.5 py-1 text-[11px] font-medium text-primary border border-primary-line rounded-lg bg-surface hover:bg-primary-subtle disabled:opacity-50 transition-colors flex-shrink-0"
                                  aria-label={`Repair the host routes of ${db.display_name}`}>
                                  <RefreshCw className={`w-3 h-3 ${repairingRoutes.has(db.instance_id) ? 'animate-spin' : ''}`} aria-hidden />
                                  {repairingRoutes.has(db.instance_id) ? 'Repairing…' : 'Repair'}
                                </button>
                              </div>
                            </td>
                          </tr>
                        )}
                        {db.status === 'delete_failed' && (
                          <tr>
                            <td colSpan={9} className="px-3 py-2 bg-err-bg text-[11px] text-err-text">
                              <span className="font-medium">Removal incomplete:</span> {db.config_error || 'part of the network path is still in place.'}
                              {(db.config_log || []).length > 0 && (
                                <pre className="mt-1 text-[10px] font-mono whitespace-pre-wrap">{db.config_log.join('\n')}</pre>
                              )}
                            </td>
                          </tr>
                        )}
                        {connectionResults[db.instance_id] && (
                          <tr>
                            <td colSpan={9} className="px-3 py-2 bg-surface-2">
                              <div className="flex items-start justify-between">
                                <pre className="text-[10px] font-mono text-fg-2 whitespace-pre-wrap">
{(() => {
  const r = connectionResults[db.instance_id]
  const lines = []
  lines.push(`connected: ${r.connected ? '✓ YES' : '✕ NO'}`)
  lines.push(`method: ${r.connection_method || 'unknown'}`)
  lines.push(`network: ${r.network_method || 'unknown'}`)
  lines.push(`secret_host: ${r.secret_host || 'none'}`)
  lines.push(`final_host: ${r.final_host || 'none'}`)
  if (r.registry_lookup) {
    lines.push(`registry_match: ${r.registry_lookup.matched_host}`)
    lines.push(`registry_port: ${r.registry_lookup.matched_port}`)
  }
  if (r.connected) {
    lines.push(`db: ${r.db_name || ''}`)
  }
  if (r.connection_error) lines.push(`error: ${r.connection_error}`)
  if (r.secret_error) lines.push(`secret_error: ${r.secret_error}`)
  if (r.error) lines.push(`error: ${r.error}`)
  return lines.join('\n')
})()}
                                </pre>
                                <button onClick={() => setConnectionResults(prev => { const n = {...prev}; delete n[db.instance_id]; return n })}
                                  className="text-[10px] text-fg-muted hover:text-err ml-2">✕</button>
                              </div>
                            </td>
                          </tr>
                        )}
                      </React.Fragment>
                      )
                    })}
                  </tbody>
                </table>
                <DbListPager c={onboardedCtl} count={onboardedPage.totalGroups} totalPages={onboardedPage.totalPages} page={onboardedPage.page} />
              </div>
            )}

            {/* Config Log — from background tasks stored in DynamoDB. Collapsed by default:
                it is a diagnostic, not something to read on every visit. */}
            {configuredDbs.some(d => d.config_log?.length) && (
              <details className="mt-4">
                <summary className="text-[10px] text-fg-muted cursor-pointer hover:text-fg-2">
                  Configuration log
                </summary>
                <pre className="mt-1 bg-surface-2 rounded p-2 text-[10px] text-fg-3 font-mono overflow-x-auto max-h-60">
                  {configuredDbs.filter(d => d.config_log?.length).map(d =>
                      `── ${d.display_name} (${d.status}${d.network_method ? ', ' + d.network_method : ''}) ──\n${d.config_log.map(l => `  ${l}`).join('\n')}`
                  ).join('\n\n')}
                </pre>
              </details>
            )}
            {configLog.length > 0 && (
              <div className="mt-2 bg-err-bg rounded p-2 text-[10px] text-err font-mono">
                {configLog.join('\n')}
                <button onClick={() => setConfigLog([])} className="ml-2 text-err hover:text-err">✕</button>
              </div>
            )}
          </div>
          <StageNav />
        </div>
      )}

      {/* ── Stage 4: Operators ── */}
      {activeTab === 'operators' && (
        <div className="space-y-6">
          {/* Access summary */}
          <div className="bg-surface rounded-lg border border-line p-6">
            <h2 className="text-sm font-semibold text-fg mb-1">Operators</h2>
            <p className="text-xs text-fg-3 mb-4">Operators are the people who can open this space in the operator app and work with its databases. Only users added here can access it.</p>
            <div className="grid grid-cols-2 gap-6">
              <div>
                <h3 className="text-xs font-semibold text-fg-2 mb-2">Operator app link</h3>
                <p className="text-xs text-fg-3 mb-2">
                  Direct link to the operator web app for this space.
                  Only users assigned below can access it.
                </p>
                {webAppUrl && (
                  <a href={webAppUrl} target="_blank" rel="noopener noreferrer"
                    className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium text-primary border border-primary rounded-lg hover:bg-primary-subtle transition-colors">
                    Open operator app ↗
                  </a>
                )}
              </div>
              <div>
                <h3 className="text-xs font-semibold text-fg-2 mb-2">Access control</h3>
                <p className="text-xs text-fg-3 mb-2">
                  Access is managed via Cognito user groups. Assign users below to grant them
                  access to this space's databases in the operator app.
                </p>
                <p className="text-[10px] text-fg-muted">
                  Users must be explicitly added to access this space.
                </p>
              </div>
            </div>
          </div>

          <WebAppUsers api={api} space={space} onCount={setOperatorCount} />
          <StageNav />
        </div>
      )}

      {/* ── Stage 5: Topology (the review step) ── */}
      {activeTab === 'topology' && (() => {
        const cov = topology?.coverage || null
        const tiles = topologyStats ? [
          [topologyStats.databases || 0, 'Databases'],
          [topologyStats.vpcs || 0, 'VPCs'],
          [(topologyStats.peering_connections || 0) + (topologyStats.lattice_connections || 0) + (topologyStats.same_vpc_connections || 0), 'Connections'],
          [cov ? cov.reachable || 0 : '—', 'Reachable', 'text-ok-text'],
          [cov ? (cov.pending || 0) + (cov.blocked || 0) : '—', 'Pending or blocked', cov && (cov.pending || cov.blocked) ? 'text-warn-text' : ''],
          [cov ? `${cov.refreshed || 0}/${cov.databases || 0}` : '—', 'Metadata refreshed'],
        ] : []
        return (
          <div className="space-y-6">
            <div className="bg-surface rounded-lg border border-line p-6">
              <div className="flex items-start justify-between gap-4 mb-4">
                <div>
                  <h2 className="text-sm font-semibold text-fg mb-1">Resource Topology</h2>
                  <p className="text-xs text-fg-3">
                    Databases, VPCs and the connection the agent has to each of them. Reachability reflects the
                    recorded state of each connection, not a live probe.
                    {topology?.generated_at && <span className="text-fg-muted"> Generated {new Date(topology.generated_at).toLocaleString()}.</span>}
                  </p>
                </div>
                <div className="flex items-center gap-2 flex-shrink-0">
                  {webAppUrl && (
                    <a href={webAppUrl} target="_blank" rel="noopener noreferrer"
                      className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-fg-2 border border-line rounded-lg hover:bg-surface-3 transition-colors">
                      Operator app <ExternalLink className="w-3.5 h-3.5" aria-hidden />
                    </a>
                  )}
                  <button onClick={refreshTopology} disabled={topologyRefreshing || !space.access_configured}
                    className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-primary border border-primary rounded-lg hover:bg-primary-subtle transition-colors disabled:opacity-50"
                    title="Collect fresh RDS, CloudWatch and network metadata for every database in this space">
                    <RefreshCw className={`w-3.5 h-3.5 ${topologyRefreshing ? 'animate-spin' : ''}`} aria-hidden />
                    {topologyRefreshing ? 'Refreshing…' : 'Refresh metadata'}
                  </button>
                </div>
              </div>

              {topologyLoading && !topology ? (
                <div className="py-8 text-center">
                  <div className="w-5 h-5 border-2 border-primary border-t-transparent rounded-full animate-spin mx-auto mb-2" />
                  <p className="text-xs text-fg-3">Loading topology...</p>
                </div>
              ) : topologyStats ? (
                <div className="space-y-4">
                  <div className="grid grid-cols-3 sm:grid-cols-6 gap-3">
                    {tiles.map(([value, label, cls]) => (
                      <div key={label} className="bg-surface-2 rounded-lg p-3 text-center">
                        <div className={`text-xl font-bold ${cls || 'text-fg'}`}>{value}</div>
                        <div className="text-[10px] text-fg-3">{label}</div>
                      </div>
                    ))}
                  </div>
                  {/* Keyed on the generation time: a newly generated map mounts fresh. */}
                  <TopologyMap key={topology.generated_at || 'none'} nodes={topology.nodes || []} edges={topology.edges || []} coverage={cov}
                    warnings={topology.warnings || []} compact exportName={`topology-${space.space_id}`} />
                </div>
              ) : (
                <div className="py-8 text-center">
                  <p className="text-sm text-fg-3">No topology data yet.</p>
                  <p className="text-xs text-fg-muted mt-1">
                    {space.access_configured ? 'Onboard databases to build the resource map.' : 'Validate access first, then onboard databases.'}
                  </p>
                </div>
              )}
            </div>
            <StageNav />
          </div>
        )
      })()}
      </fieldset>

      {/* ── Audit: outside the fieldset, so reading it never depends on canManage ── */}
      {activeTab === 'audit' && canViewAudit && (
        <AuditLog api={api} spaceId={space.space_id} isSuperAdmin={isSuperAdmin} />
      )}

      {/* ── Add Secondary Account Wizard Modal ── */}
      {showAddAccount && (
        <SideSheet title="Add secondary cloud source" onClose={() => setShowAddAccount(false)}
          subtitle="Connect another AWS account to this Agent Space: your access role must be in that account, then register the account here."
          header={<StepIndicator step={addAccountStep} labels={['Set up the role', 'Connect to the Agent Space']} />}
          footer={(
            <div className="flex items-center justify-between">
              <button onClick={() => setShowAddAccount(false)}
                className="text-sm text-fg-2 hover:text-fg">Cancel</button>
              <div className="flex items-center gap-2">
                {addAccountStep === 2 && (
                  <button onClick={() => setAddAccountStep(1)}
                    className="px-4 py-2 text-sm text-fg-2 border border-line rounded-lg hover:bg-surface-3">
                    Previous
                  </button>
                )}
                {addAccountStep === 1 && (
                  <button onClick={() => setAddAccountStep(2)} disabled={!accountIdValid || !newRoleName.trim()}
                    title={!accountIdValid ? 'Enter the 12-digit account ID first' : ''}
                    className="px-4 py-2 text-sm font-medium bg-primary text-primary-fg rounded-lg hover:bg-primary-hover disabled:opacity-50 disabled:cursor-not-allowed">
                    Next
                  </button>
                )}
                {addAccountStep === 2 && (
                  <button onClick={handleAddAccount} disabled={addingAccount || !newAccountId.trim()}
                    className="px-4 py-2 text-sm font-medium bg-primary text-primary-fg rounded-lg hover:bg-primary-hover disabled:opacity-50">
                    {addingAccount ? 'Adding...' : 'Add account'}
                  </button>
                )}
              </div>
            </div>
          )}>
            {/* Step 1: create the role — one prefilled command, manual walkthrough folded away */}
            {addAccountStep === 1 && (
              <div className="px-6 py-4 space-y-5">
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                  <div>
                    <label className="block text-xs font-medium text-fg-2 mb-1" htmlFor="add-account-id">Secondary account ID *</label>
                    <input id="add-account-id" type="text" inputMode="numeric" value={newAccountId} onChange={(e) => setNewAccountId(e.target.value.replace(/\s/g, ''))}
                      className="w-full px-3 py-2 text-sm font-mono border border-line-strong rounded-lg bg-surface text-fg focus:outline-none focus:ring-2 focus:ring-primary"
                      placeholder="123456789012" autoFocus />
                    <p className={`text-[11px] mt-1 ${newAccountId && !accountIdValid ? 'text-warn-mid' : 'text-fg-muted'}`}>
                      {newAccountId && !accountIdValid ? 'Account IDs are 12 digits.' : 'The AWS account that holds the databases.'}
                    </p>
                  </div>
                  <div>
                    <div className="flex items-center justify-between mb-1">
                      <label className="block text-xs font-medium text-fg-2" htmlFor="add-account-role">Role name</label>
                      {!roleNameEditable && (
                        <button type="button" onClick={() => setRoleNameEditable(true)}
                          className="text-[11px] text-primary hover:underline">Use a different name</button>
                      )}
                    </div>
                    <input id="add-account-role" type="text" value={newRoleName} onChange={(e) => setNewRoleName(e.target.value)}
                      readOnly={!roleNameEditable}
                      className={`w-full px-3 py-2 text-sm font-mono border border-line-strong rounded-lg text-fg focus:outline-none focus:ring-2 focus:ring-primary ${roleNameEditable ? 'bg-surface' : 'bg-surface-2 text-fg-2'}`}
                      placeholder={suggestedRoleName(space)} />
                    <p className="text-[11px] text-fg-muted mt-1">
                      {roleNameEditable
                        ? <>Only if your naming policy requires it. Keep the <code className="font-mono">{config.resourcePrefix}-agent-</code> start: the deployment assumes only <code className="font-mono">{config.resourcePrefix}-*</code> roles, and the version check reads <code className="font-mono">{config.resourcePrefix}-agent-*</code> roles.</>
                        : 'Your access role. One per account, shared by all your spaces.'}
                    </p>
                  </div>
                </div>

                {accountIdValid && accountCheck?.loading && (
                  <p className="flex items-center gap-2 text-xs text-fg-3" role="status">
                    <RefreshCw className="w-3.5 h-3.5 animate-spin" aria-hidden /> Checking account {newAccountId.trim()}…
                  </p>
                )}
                {accountIdValid && accountCheck?.error && (
                  <p className="text-xs text-warn-mid" role="status">
                    Could not check this account: {accountCheck.error} You can still set it up with the command below.
                  </p>
                )}

                {accountIdValid && checkState === 'ready' && (
                  <div className="space-y-3">
                    <div className="flex items-start gap-2 bg-ok-bg border border-ok-border rounded-lg p-3 text-xs text-ok-text" role="status">
                      <Check className="w-4 h-4 shrink-0 mt-px" aria-hidden />
                      <span>
                        Your access role is already in this account
                        {accountCheck.result.spaces?.length ? ` (used by your space${accountCheck.result.spaces.length > 1 ? 's' : ''} ${accountCheck.result.spaces.join(', ')})` : ''}.
                        {' '}Nothing to run.
                      </span>
                    </div>
                    <details className="rounded-lg border border-line">
                      <summary className="px-4 py-2.5 text-xs font-medium text-fg-2 cursor-pointer hover:bg-surface-2 rounded-lg select-none">
                        Update the role's permissions
                      </summary>
                      <div className="px-4 pb-4 pt-1">
                        <div className="flex items-center justify-end mb-1.5">
                          <button onClick={() => copyText(memberRoleUpdateCli, 'cli-update')}
                            className="inline-flex items-center gap-1 px-2.5 py-1 text-[11px] font-medium text-primary border border-primary-line rounded-lg hover:bg-primary-subtle transition-colors">
                            {copiedField === 'cli-update' ? <><Check className="w-3 h-3" aria-hidden /> Copied</> : <><Copy className="w-3 h-3" aria-hidden /> Copy command</>}
                          </button>
                        </div>
                        <pre className="bg-surface-code text-ok rounded-lg p-3 text-[10px] overflow-x-auto max-h-64 whitespace-pre">{memberRoleUpdateCli}</pre>
                        <p className="text-[11px] text-fg-3 mt-1.5">Re-applies the read-only permission policy and records its version (<code className="bg-surface-3 px-1 rounded">{POLICY_VERSION_TAG}={MEMBER_POLICY_VERSION}</code>). Not needed today.</p>
                      </div>
                    </details>
                  </div>
                )}

                {accountIdValid && checkState === 'outdated' && (
                  <div className="space-y-3">
                    <div className="flex items-start gap-2 bg-warn-bg border border-warn-border rounded-lg p-3 text-xs text-warn-text" role="status">
                      <AlertTriangle className="w-4 h-4 shrink-0 mt-px" aria-hidden />
                      <span>Your role is there, but its permissions are older than this version. Run this in account <span className="font-mono">{newAccountId.trim()}</span> to update them.</span>
                    </div>
                    <div>
                      <div className="flex flex-wrap items-center justify-between gap-2 mb-1.5">
                        <p className="text-xs font-medium text-fg-2">Update the role's permissions</p>
                        <div className="flex items-center gap-3">
                          <a href={cloudShellUrl} target="_blank" rel="noopener noreferrer"
                            className="text-[11px] text-primary hover:underline inline-flex items-center gap-1" title="Opens CloudShell for whichever account you are signed in to">
                            <ExternalLink className="w-3 h-3" aria-hidden /> Open CloudShell
                          </a>
                          <button onClick={() => copyText(memberRoleUpdateCli, 'cli-update')}
                            className="inline-flex items-center gap-1 px-2.5 py-1 text-[11px] font-medium text-primary border border-primary-line rounded-lg hover:bg-primary-subtle transition-colors">
                            {copiedField === 'cli-update' ? <><Check className="w-3 h-3" aria-hidden /> Copied</> : <><Copy className="w-3 h-3" aria-hidden /> Copy command</>}
                          </button>
                        </div>
                      </div>
                      <pre className="bg-surface-code text-ok rounded-lg p-3 text-[10px] overflow-x-auto max-h-64 whitespace-pre">{memberRoleUpdateCli}</pre>
                    </div>
                  </div>
                )}

                {accountIdValid && checkState === 'denied' && (
                  <div className="space-y-3">
                    <div className="flex items-start gap-2 bg-err-bg border border-err-border rounded-lg p-3 text-xs text-err-text" role="alert">
                      <AlertTriangle className="w-4 h-4 shrink-0 mt-px" aria-hidden />
                      <span>{accountCheck.result.message}</span>
                    </div>
                    {accountCheck.result.fix === 'trust' && (
                      <div>
                        <div className="flex flex-wrap items-center justify-between gap-2 mb-1.5">
                          <p className="text-xs font-medium text-fg-2">Re-apply the trust policy in account <span className="font-mono">{newAccountId.trim()}</span></p>
                          <button onClick={() => copyText(memberRoleTrustCli, 'cli-trust')}
                            className="inline-flex items-center gap-1 px-2.5 py-1 text-[11px] font-medium text-primary border border-primary-line rounded-lg hover:bg-primary-subtle transition-colors">
                            {copiedField === 'cli-trust' ? <><Check className="w-3 h-3" aria-hidden /> Copied</> : <><Copy className="w-3 h-3" aria-hidden /> Copy command</>}
                          </button>
                        </div>
                        <pre className="bg-surface-code text-ok rounded-lg p-3 text-[10px] overflow-x-auto max-h-64 whitespace-pre">{memberRoleTrustCli}</pre>
                      </div>
                    )}
                  </div>
                )}

                {(!accountIdValid || checkState === 'missing' || accountCheck?.error) && (<>
                {accountIdValid && checkState === 'missing' && (
                  <p className="text-xs text-fg-3" role="status">{accountCheck.result.message} Create it with the command below.</p>
                )}
                <div className={accountIdValid ? '' : 'opacity-60'} aria-disabled={!accountIdValid}>
                  <div className="flex flex-wrap items-center justify-between gap-2 mb-1.5">
                    <p className="text-xs font-medium text-fg-2">
                      Run this with credentials for account <span className="font-mono">{accountIdValid ? newAccountId.trim() : '…'}</span>
                    </p>
                    <div className="flex items-center gap-3">
                      <a href={cloudShellUrl} target="_blank" rel="noopener noreferrer"
                        className="text-[11px] text-primary hover:underline inline-flex items-center gap-1" title="Opens CloudShell for whichever account you are signed in to">
                        <ExternalLink className="w-3 h-3" aria-hidden /> Open CloudShell
                      </a>
                      <button onClick={() => copyText(memberRoleCli, 'cli-role')} disabled={!accountIdValid}
                        className="inline-flex items-center gap-1 px-2.5 py-1 text-[11px] font-medium text-primary border border-primary-line rounded-lg hover:bg-primary-subtle disabled:opacity-50 transition-colors">
                        {copiedField === 'cli-role' ? <><Check className="w-3 h-3" aria-hidden /> Copied</> : <><Copy className="w-3 h-3" aria-hidden /> Copy command</>}
                      </button>
                    </div>
                  </div>
                  <pre className="bg-surface-code text-ok rounded-lg p-3 text-[10px] overflow-x-auto max-h-64 whitespace-pre">{memberRoleCli}</pre>
                  <p className="text-[11px] text-fg-3 mt-1.5">
                    Creates the role with this space's trust policy and attaches the read-only permission policy — nothing to edit.
                    Safe to re-run: the policy and its version tag are applied in place; <code className="bg-surface-3 px-1 rounded">create-role</code> reports <code className="bg-surface-3 px-1 rounded">EntityAlreadyExists</code> if the role is already there.
                    Bash quoting; on Windows use CloudShell or a bash shell.
                  </p>
                </div>

                <details className="rounded-lg border border-line">
                  <summary className="px-4 py-2.5 text-xs font-medium text-fg-2 cursor-pointer hover:bg-surface-2 rounded-lg select-none">
                    Do it manually in the IAM console instead
                  </summary>
                  <div className="px-4 pb-4 pt-1">
                    <p className="text-xs text-fg-3 mb-4">Same result as the command: create the role with this trust policy, then attach the permission policy.</p>
                    <div className="space-y-5">
                      <div>
                        <p className="text-xs font-medium text-fg-2 mb-1">Step 1: Go to IAM → Roles → Create Role</p>
                        <p className="text-xs text-fg-3">Select "Custom trust policy" as the trusted entity type.</p>
                      </div>

                      <div>
                        <div className="flex items-center justify-between mb-1">
                          <p className="text-xs font-medium text-fg-2">Step 2: Paste this custom trust policy</p>
                          <button onClick={() => { navigator.clipboard.writeText(JSON.stringify(memberRoleTrustPolicy(space), null, 2)); setCopiedField('trust'); setTimeout(() => setCopiedField(null), 2000) }}
                            className="text-[10px] text-primary hover:underline flex items-center gap-1">{copiedField === 'trust' ? <><Check className="w-3 h-3" aria-hidden /> Copied</> : <><Copy className="w-3 h-3" aria-hidden /> Copy</>}</button>
                        </div>
                        <pre className="bg-surface-code text-ok rounded p-3 text-[10px] overflow-x-auto">
{JSON.stringify(memberRoleTrustPolicy(space), null, 2)}
                        </pre>
                        <p className="text-[10px] text-fg-3 mt-1">
                          The <code className="bg-surface-3 px-1 rounded">sts:ExternalId</code> is the id of
                          the admin who owns this space. One role serves every space that admin owns in
                          this account, and no other admin's space can use it even with the account number
                          and role name. The <code className="bg-surface-3 px-1 rounded">aws:PrincipalArn</code> condition
                          restricts the trust to IAM roles in the agent's account, so a user or federated
                          session there cannot assume it even with the id.
                        </p>
                      </div>

                      <div>
                        <p className="text-xs font-medium text-fg-2 mb-1">Step 3: Skip "Add permissions" — click Next</p>
                        <p className="text-xs text-fg-3">We'll add the inline policy after the role is created.</p>
                      </div>

                      <div>
                        <div className="flex items-center justify-between mb-1">
                          <p className="text-xs font-medium text-fg-2">Step 4: Name the role and create it</p>
                          <button onClick={() => { navigator.clipboard.writeText(suggestedRoleName(space)); setCopiedField('role'); setTimeout(() => setCopiedField(null), 2000) }}
                            className="text-[10px] text-primary hover:underline flex items-center gap-1">{copiedField === 'role' ? <><Check className="w-3 h-3" aria-hidden /> Copied</> : <><Copy className="w-3 h-3" aria-hidden /> Copy</>}</button>
                        </div>
                        <code className="block bg-surface-2 rounded p-2 text-xs text-fg-2 font-mono">
                          {suggestedRoleName(space)}
                        </code>
                        <p className="text-[10px] text-fg-muted mt-1">One role per admin per account, shared by all your spaces (stays under IAM's 64-char limit).</p>
                      </div>

                      <div>
                        <div className="flex items-center justify-between mb-1">
                          <p className="text-xs font-medium text-fg-2">Step 5: Open the role → Add permissions → Create inline policy → JSON tab → Paste:</p>
                          <button onClick={() => { navigator.clipboard.writeText(JSON.stringify(secondaryAccountPolicy(config.resourcePrefix, accountIdValid ? newAccountId.trim() : '*', ownerAdminOf(space)), null, 2)); setCopiedField('policy'); setTimeout(() => setCopiedField(null), 2000) }}
                            className="text-[10px] text-primary hover:underline flex items-center gap-1">{copiedField === 'policy' ? <><Check className="w-3 h-3" aria-hidden /> Copied</> : <><Copy className="w-3 h-3" aria-hidden /> Copy</>}</button>
                        </div>
                        <pre className="bg-surface-code text-ok rounded p-3 text-[10px] overflow-x-auto max-h-40">
{JSON.stringify(secondaryAccountPolicy(config.resourcePrefix, accountIdValid ? newAccountId.trim() : '*', ownerAdminOf(space)), null, 2)}
                        </pre>
                        <p className="text-xs text-fg-3 mt-1">Name the policy: <code className="bg-surface-3 px-1 rounded">{SUGGESTED_POLICY_NAME}</code></p>
                      </div>

                      <div>
                        <p className="text-xs font-medium text-fg-2 mb-1">Step 6: Tags → add <code className="bg-surface-3 px-1 rounded">{POLICY_VERSION_TAG}</code> = <code className="bg-surface-3 px-1 rounded">{MEMBER_POLICY_VERSION}</code></p>
                        <p className="text-xs text-fg-3">Records which version of the policy the role carries, so this wizard can tell when it needs an update.</p>
                      </div>

                    </div>
                  </div>
                </details>
                </>)}
              </div>
            )}

            {/* Step 2: register the account — everything is known, just confirm */}
            {addAccountStep === 2 && (
              <div className="px-6 py-4 space-y-4">
                <p className="text-xs text-fg-3">
                  Once the role exists in the secondary account, register it here. Access is validated from the Setup checklist
                  (Access → Reconfigure), which assumes the role and reports any problem.
                </p>
                <div className="rounded-lg border border-line divide-y divide-line-2 text-xs">
                  <div className="flex items-center justify-between gap-4 px-4 py-2.5"><span className="text-fg-muted">Account ID</span><span className="font-mono text-fg-2">{newAccountId.trim()}</span></div>
                  <div className="flex items-center justify-between gap-4 px-4 py-2.5"><span className="text-fg-muted">Role</span><span className="font-mono text-fg-2 truncate" title={`arn:aws:iam::${newAccountId.trim()}:role/${newRoleName.trim()}`}>arn:aws:iam::{newAccountId.trim()}:role/{newRoleName.trim()}</span></div>
                  <div className="flex items-center justify-between gap-4 px-4 py-2.5"><span className="text-fg-muted">External ID</span><span className="font-mono text-fg-2">{ownerAdminOf(space)}</span></div>
                  <div className="flex items-center justify-between gap-4 px-4 py-2.5"><span className="text-fg-muted">Trusted by</span><span className="font-mono text-fg-2">roles in account {space.primary_account_id}</span></div>
                </div>
                {addAccountError && (
                  <div className="bg-err-bg border border-err-border rounded-lg p-3 text-xs text-err-text">{addAccountError}</div>
                )}
              </div>
            )}
        </SideSheet>
      )}

      {/* ── Configure Database Wizard Modal ── */}
      {configDbWizard && (
        <ConfigureDatabaseModal
          space={space}
          state={configDbWizard}
          update={updateWizard}
          onClose={closeConfigDbWizard}
          onTest={handleTestCredentials}
          onSubmit={handleSubmitConfigDb}
          copyHelper={(value, key) => { navigator.clipboard.writeText(value); setCopiedField(key); setTimeout(() => setCopiedField(null), 2000) }}
          copiedField={copiedField}
        />
      )}
    </div>
  )
}
