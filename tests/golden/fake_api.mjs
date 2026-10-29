// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
//
// A stateful stand-in for backend/app.py, answering every /api/** call the operator app
// and the admin console make, from an in-memory model of spaces, databases, operators,
// users, secrets, audit records and network state.
//
// It mirrors the real backend's behaviour, not just its shapes:
//   * state moves on over time the way the background tasks move it: an onboard is
//     `configuring` then `configured` (and only then a member), a removal is `deleting`
//     then gone (or `delete_failed` when its teardown is set to fail), a space deletion
//     is `deleting` then gone, a failover swaps the roles, a route repair clears its flag;
//   * it refuses what the real backend refuses, with the real backend's messages: a
//     secret outside the owner's folder, a secret whose host is not the database's
//     endpoint, an unsupported database, a manage call by anyone but the owner or a
//     super admin, an audit vault switch-off without the typed phrase;
//   * a route it does not implement answers 501 and is recorded in `unhandled`, which
//     fails the scenario: the fake cannot silently drift from the API.
//
// Identity comes from the request's (unsigned) JWT exactly as the backend reads it: `sub`
// and `cognito:groups`. Operator membership is the `space-<id>` group, so a user added to
// a space sees it only in a token minted after the grant (tokenFor), as with Cognito.

const PREFIX = 'dbops'
export const DEPLOYMENT_ACCOUNT = '111122223333'
export const REGION = 'us-east-1'
export const GLOBAL_DB_REASON = 'Not supported yet: part of an Aurora Global Database'
export const VAULT_OFF_PHRASE = 'turn off audit vault'
// MEMBER_POLICY_VERSION in backend/app.py and SpaceDetail.jsx (the DBOpsPolicyVersion tag).
export const MEMBER_POLICY_VERSION = '2'

// code -> [label, target_type]; the subset of tools/aws/audit_log.py ACTIONS the fake writes.
const ACTIONS = {
  space_create: ['Created space', 'space'],
  space_update: ['Changed space details', 'space'],
  space_delete: ['Deleted space', 'space'],
  space_delete_teardown_complete: ['Finished deleting space', 'space'],
  space_access_configure: ['Validated account access', 'space'],
  space_discover: ['Ran discovery', 'space'],
  space_candidate_dismiss: ['Dismissed discovered database', 'database'],
  space_db_onboard: ['Onboarded database', 'database'],
  database_space_remove: ['Removed database from space', 'database'],
  database_teardown_complete: ['Finished removing database', 'database'],
  database_teardown_failed: ['Database removal incomplete', 'database'],
  cluster_failover_detected: ['Detected cluster failover', 'database'],
  network_host_routes_repaired: ['Repaired cross-Region host routes', 'database'],
  space_user_assign: ['Added operator', 'user'],
  space_user_remove: ['Removed operator', 'user'],
  audit_vault_set: ['Switched audit vault', 'setting'],
}

const ownerKey = (sub) => (sub || '').replace(/-/g, '').slice(0, 12).toLowerCase() || 'unknown'
const b64url = (o) => Buffer.from(JSON.stringify(o)).toString('base64url')
const now = () => new Date().toISOString()
const clone = (o) => JSON.parse(JSON.stringify(o))

class HttpError extends Error {
  constructor(status, detail) { super(detail); this.status = status; this.detail = detail }
}

// The databases RDS "has" in the deployment account, which discovery finds.
function defaultWorld() {
  const ep = (id, kind = '') => `${id}.${kind}c0ffee00.${REGION}.rds.amazonaws.com`
  return [
    { id: 'orders-db', engine: 'rds-postgresql', endpoint: ep('orders-db') },
    { id: 'orders-replica', engine: 'rds-postgresql', endpoint: ep('orders-replica'),
      replica_of: 'orders-db', replica_source_endpoint: ep('orders-db') },
    { id: 'billing-cluster', engine: 'aurora-postgresql', endpoint: ep('billing-cluster', 'cluster-'),
      reader_endpoint: ep('billing-cluster', 'cluster-ro-'),
      members: [
        { instance_id: 'billing-1', is_writer: true, endpoint: ep('billing-1'), availability_zone: 'us-east-1a' },
        { instance_id: 'billing-2', is_writer: false, endpoint: ep('billing-2'), availability_zone: 'us-east-1b', replica_lag_ms: 18 },
      ] },
    { id: 'ledger-global', engine: 'aurora-postgresql', endpoint: ep('ledger-global', 'cluster-'), unsupported: GLOBAL_DB_REASON },
  ]
}

export class FakeBackend {
  constructor({ delays = {}, world = defaultWorld() } = {}) {
    this.delays = { configure: 2500, teardown: 2500, spaceTeardown: 2500, ...delays }
    this.world = world
    this.users = new Map()       // sub -> { sub, email, name, role }
    this.spaces = new Map()      // space_id -> space
    this.instances = new Map()   // instance_id -> record
    this.secrets = new Map()     // arn -> { name, host, username, password }
    this.audit = []              // newest last
    this.auditSettings = { vault_enabled: true, retention_days: 365, floor_days: 365, effective_retention_days: 365, configured: false }
    this.timers = []             // { at, run }
    this.requests = []           // { method, path, user, status }
    this.unhandled = []          // 'METHOD /path' the fake does not implement
    this.failures = []           // refusals: { method, path, status, detail, stage }
    this.memberAccounts = new Map() // account_id -> Map(role name -> { trusts: Set(owner sub), version })
    this.seq = 0
  }

  // ── fixtures ──────────────────────────────────────────────────────────────
  addUser(key, { email, name, role }) {
    const sub = `${key}-${'0'.repeat(8)}-4000-8000-${String(++this.seq).padStart(12, '0')}`
    this.users.set(sub, { sub, email, name: name || email.split('@')[0], role, key })
    return sub
  }
  userByKey(key) { return [...this.users.values()].find(u => u.key === key) }
  // The Cognito groups a token minted now would carry.
  groupsOf(sub) {
    const u = this.users.get(sub)
    const groups = u.role ? [u.role] : []
    for (const s of this.spaces.values()) if (s.operators.has(sub)) groups.push(`space-${s.space_id}`)
    return groups
  }
  tokenFor(key) {
    const u = this.userByKey(key)
    if (!u) throw new Error(`no fixture user ${key}`)
    return `${b64url({ alg: 'none', typ: 'JWT' })}.${b64url({ sub: u.sub, email: u.email, username: u.sub, name: u.name,
      exp: Math.floor(Date.now() / 1000) + 3600, 'cognito:groups': this.groupsOf(u.sub) })}.`
  }
  newId() {
    const n = String(++this.seq).padStart(12, '0')
    return `5ace0000-0000-4000-8000-${n}`
  }
  // A secret in Secrets Manager; returns its ARN.
  putSecret(name, { host = '', username = 'dbops_ro', password = 'x' } = {}) {
    const arn = `arn:aws:secretsmanager:${REGION}:${DEPLOYMENT_ACCOUNT}:secret:${name}-AbC123`
    this.secrets.set(arn, { name, host, username, password })
    return arn
  }
  secretFolder(space) { return `${PREFIX}/${space.owner_admin}/` }
  secretNameFor(space, dbId, engine = 'postgres') { return `${this.secretFolder(space)}${engine}/${dbId}/readonly` }
  // A space created directly in the model (a fixture), owned by `ownerKey`.
  seedSpace(ownerUserKey, { name, regions = [REGION], access = true, operators = [] }) {
    const owner = this.userByKey(ownerUserKey)
    const id = this.newId()
    this.spaces.set(id, this._spaceRecord(id, owner, { name, regions, access_configured: access }))
    for (const k of operators) this.spaces.get(id).operators.add(this.userByKey(k).sub)
    return id
  }
  // The per-admin access role's name (agent_role_name): `<prefix>-agent-<owner key>`.
  agentRoleName(ownerSub) { return `${PREFIX}-agent-${ownerKey(ownerSub)}` }
  // A role in a member (secondary) account, as IAM there holds it: whose ExternalId its
  // trust policy accepts (owner admin subs), and its DBOpsPolicyVersion tag (null: none).
  putMemberRole(accountId, roleName, { trusts = [], version = null } = {}) {
    if (!this.memberAccounts.has(accountId)) this.memberAccounts.set(accountId, new Map())
    this.memberAccounts.get(accountId).set(roleName, { trusts: new Set(trusts), version })
  }
  // A secondary account already registered on a space (a fixture), with its status.
  seedAccount(spaceId, accountId, { status = 'valid', roleName = '' } = {}) {
    const s = this.spaces.get(spaceId)
    s.secondary_account_ids = [...s.secondary_account_ids, accountId]
    if (roleName) s.secondary_role_names = { ...s.secondary_role_names, [accountId]: roleName }
    if (status) s.account_statuses = { ...s.account_statuses, [accountId]: status }
  }
  // A database already onboarded into `spaceIds` (a fixture), from the world list.
  seedMember(worldId, spaceIds, extra = {}) {
    const w = this.world.find(x => x.id === worldId)
    const rec = this._instanceRecord(w)
    rec.status = 'configured'
    rec.member_space_ids = [...spaceIds]
    rec.reach_space_id = spaceIds[0]
    Object.assign(rec, extra)
    this.instances.set(rec.instance_id, rec)
    for (const s of spaceIds) this._refreshCount(s)
    return rec.instance_id
  }

  // What discovery does for a space (POST /discover without the audit line); also used
  // to seed a scenario that starts after discovery.
  discoverFor(spaceId) {
    const s = this.spaces.get(spaceId)
    const notSupported = []
    let added = 0
    for (const w of this.world) {
      if (w.unsupported) { notSupported.push({ name: w.id, region: REGION, account: DEPLOYMENT_ACCOUNT, engine: w.engine, reason: w.unsupported }); continue }
      const id = `db-${w.id}`
      let rec = this.instances.get(id)
      if (!rec) { rec = this._instanceRecord(w); this.instances.set(id, rec); added++ }
      if (!rec.member_space_ids.includes(s.space_id) && !rec.candidate_space_ids.includes(s.space_id)) rec.candidate_space_ids.push(s.space_id)
    }
    s.unsupported_databases = notSupported
    s.last_discovery_at = now()
    return { method: 'rds-api', total_discovered: this.world.length, new_added: added, proposed_existing: 0, stale_removed: 0, stale_kept: 0,
      regions_scanned: s.regions, accounts_scanned: [DEPLOYMENT_ACCOUNT], scan_log: [`accounts: ['${DEPLOYMENT_ACCOUNT}'], regions: ${JSON.stringify(s.regions)}`], databases: [], not_supported: notSupported }
  }

  // ── events the backend's background work produces ───────────────────────────
  // What the Tier 1 collector does on a failover: swap the writer, record it once.
  failover(instanceId) {
    const rec = this.instances.get(instanceId)
    const roles = rec.cluster_member_roles
    const oldW = roles.find(m => m.is_writer)
    const newW = roles.find(m => !m.is_writer && !m.skip_reason)
    oldW.is_writer = false
    newW.is_writer = true
    oldW.replica_lag_ms = newW.replica_lag_ms ?? 20
    delete newW.replica_lag_ms
    rec.writer_instance_id = newW.instance_id
    rec.members_changed_at = now()
    for (const s of rec.member_space_ids) {
      this._audit(null, 'cluster_failover_detected', { space: this.spaces.get(s), target: rec,
        before: { writer_instance_id: oldW.instance_id }, after: { writer_instance_id: newW.instance_id, previous_writer_instance_id: oldW.instance_id } })
    }
  }

  // Applies every background transition that is due.
  tick() {
    const t = Date.now()
    const due = this.timers.filter(x => x.at <= t)
    this.timers = this.timers.filter(x => x.at > t)
    for (const d of due) d.run()
  }
  _later(ms, run) { this.timers.push({ at: Date.now() + ms, run }) }

  // ── records ─────────────────────────────────────────────────────────────────
  _spaceRecord(id, owner, extra = {}) {
    return { space_id: id, name: '', description: '', tags: {}, created_by: owner.sub, owner_admin: owner.sub,
      primary_account_id: DEPLOYMENT_ACCOUNT, regions: [], secondary_account_ids: [], secondary_role_names: {},
      account_statuses: {}, access_configured: false, status: 'active', database_count: 0,
      unsupported_databases: [], created_at: now(), updated_at: now(), operators: new Set(), ...extra }
  }
  _instanceRecord(w) {
    const cluster = !!w.members
    return {
      instance_id: `db-${w.id}`, display_name: w.id, engine: w.engine, region: REGION, account_id: DEPLOYMENT_ACCOUNT,
      endpoint: w.endpoint, reader_endpoint: w.reader_endpoint || '', port: 5432, database_name: 'postgres',
      status: 'discovered', member_space_ids: [], candidate_space_ids: [], reach_space_id: '',
      tags: { raw_identifier: w.id, arn: `arn:aws:rds:${REGION}:${DEPLOYMENT_ACCOUNT}:${cluster ? 'cluster' : 'db'}:${w.id}`,
        ...(w.replica_of ? { replica_of: w.replica_of, replica_source_endpoint: w.replica_source_endpoint } : {}) },
      cluster_member_roles: cluster ? clone(w.members) : [],
      writer_instance_id: cluster ? w.members.find(m => m.is_writer).instance_id : '',
      unsupported_reason: w.unsupported || '', secret_arn: '', space_secrets: {}, config_log: [], config_error: '',
      network_method: 'peering', tier1_enabled: true, tier1_frequency: '5min', tier2_enabled: true, tier2_frequency: 'daily',
      retention_days: 30, last_health_score: 'ok', fail_teardown: 0, route_repair_needed: null,
    }
  }
  _refreshCount(spaceId) {
    const s = this.spaces.get(spaceId)
    if (s) s.database_count = [...this.instances.values()].filter(i => i.member_space_ids.includes(spaceId) && i.status === 'configured').length
  }
  _ownerEmail(space) { return this.users.get(space.created_by)?.email || '' }
  _publicSpace(space, caller) {
    const { operators, ...s } = space
    const out = { ...clone(s), owner: this._ownerEmail(space) }
    if (!this._canManage(caller, space)) delete out.unsupported_databases
    return out
  }
  _memberView(rec, spaceId) {
    return { instance_id: rec.instance_id, display_name: rec.display_name, engine: rec.engine, region: rec.region,
      account_id: rec.account_id, endpoint: rec.endpoint, reader_endpoint: rec.reader_endpoint, port: rec.port,
      status: rec.status, member_space_ids: [...rec.member_space_ids], member_count: rec.member_space_ids.length,
      tags: clone(rec.tags), cluster_member_roles: rec.cluster_member_roles.map(m => ({ instance_id: m.instance_id, is_writer: m.is_writer, endpoint: m.endpoint })),
      config_log: [...rec.config_log], config_error: rec.config_error, network_method: rec.network_method,
      tier1_enabled: rec.tier1_enabled, tier1_frequency: rec.tier1_frequency, tier2_enabled: rec.tier2_enabled,
      tier2_frequency: rec.tier2_frequency, retention_days: rec.retention_days, route_repair_needed: rec.route_repair_needed,
      teardown_stalled: false, space_id: spaceId }
  }
  _candidateView(rec, spaceId) {
    const elsewhere = rec.member_space_ids.filter(s => s !== spaceId)
    return { instance_id: rec.instance_id, display_name: rec.display_name, raw_identifier: rec.tags.raw_identifier,
      account_id: rec.account_id, region: rec.region, engine: rec.engine, endpoint: rec.endpoint, port: rec.port,
      status: rec.status === 'configured' ? 'discovered' : rec.status, database_name: rec.database_name,
      onboarded_elsewhere: elsewhere.length > 0, shared_with_count: elsewhere.length,
      suggested_secret_arn: '', suggested_secret_from: '', replica_of: rec.tags.replica_of || '',
      unsupported_reason: rec.unsupported_reason, config_error: rec.config_error, teardown_stalled: false,
      tags: clone(rec.tags) }
  }
  _operatorView(rec, spaceId) {
    const space = this.spaces.get(spaceId)
    return { instance_id: rec.instance_id, display_name: rec.display_name, engine: rec.engine, region: rec.region,
      account_id: rec.account_id, endpoint: rec.endpoint, reader_endpoint: rec.reader_endpoint, status: rec.status,
      last_health_score: rec.last_health_score, member_space_ids: [spaceId], spaces: [{ id: spaceId, name: space?.name || '' }],
      cluster_member_roles: rec.cluster_member_roles.map(m => ({ instance_id: m.instance_id, is_writer: m.is_writer, endpoint: m.endpoint })),
      tier1_enabled: true, tier1_frequency: rec.tier1_frequency, tier1_state: 'fresh', tier1_last_at: now(),
      tier2_enabled: true, tier2_frequency: rec.tier2_frequency, tier2_state: 'fresh', tier2_last_at: now(),
      retention_days: rec.retention_days, tags: { raw_identifier: rec.tags.raw_identifier, ...(rec.tags.replica_of ? { replica_of: rec.tags.replica_of } : {}) } }
  }

  // ── identity and permission (mirrors _can_manage_space and friends) ─────────
  _caller(headers) {
    const auth = headers.authorization || headers.Authorization || ''
    const tok = auth.replace(/^Bearer\s+/i, '')
    try {
      const c = JSON.parse(Buffer.from(tok.split('.')[1], 'base64url').toString())
      const u = this.users.get(c.sub)
      return { sub: c.sub, email: c.email, groups: c['cognito:groups'] || [], user: u }
    } catch { return null }
  }
  _isSuper(c) { return !!c && c.groups.includes('admins') }
  _isAdmin(c) { return !!c && (c.groups.includes('admins') || c.groups.includes('space-admins')) }
  _canManage(c, space) { return !!c && !!space && (this._isSuper(c) || (c.groups.includes('space-admins') && space.created_by === c.sub)) }
  _canView(c, space) {
    if (!c || !space) return false
    if (this._isSuper(c)) return true
    return c.groups.includes(`space-${space.space_id}`) || (c.groups.includes('space-admins') && space.created_by === c.sub)
  }
  _space(id, c, { manage = false } = {}) {
    const s = this.spaces.get(id)
    if (!s) throw new HttpError(404, 'Space not found')
    if (manage ? !this._canManage(c, s) : !this._canView(c, s)) throw new HttpError(403, manage ? 'Admin access required' : 'Access to this space is required')
    return s
  }
  _audit(c, action, { space = null, target = null, before = null, after = null } = {}) {
    const [label, ttype] = ACTIONS[action] || [action, 'space']
    const role = !c ? 'system' : this._isSuper(c) ? 'super_admin' : 'space_admin'
    this.audit.push({ ts: new Date(Date.now() + this.audit.length).toISOString(), actor_sub: c?.sub || 'system', actor_email: c?.email || '',
      actor_role: role, action, action_label: label, target_type: ttype,
      target_id: target?.instance_id || target?.space_id || target?.sub || action, target_label: target?.display_name || target?.name || target?.email || '',
      space_id: space?.space_id || '', space_name: space?.name || '',
      vault: this.auditSettings.vault_enabled ? 'stored' : 'off',
      on_other_owner: !!c && !!space && this._isSuper(c) && space.created_by !== c.sub,
      before, after })
  }
  // The secret-folder rule (_secret_namespace_refusal), word for word.
  _namespaceRefusal(space, arn) {
    const want = this.secretFolder(space)
    const name = arn.split(':secret:').slice(1).join(':secret:').replace(/-[A-Za-z0-9]{6}$/, '')
    if (name.startsWith(want)) return ''
    return `This secret is named '${name}', but this space's access role can only read secrets whose name starts with '${want}'. ` +
      `Create the secret with the name shown in Step 1 of the wizard (it starts with '${want}') and paste that ARN.`
  }
  _allowedHosts(rec) {
    const hosts = new Set([rec.endpoint].filter(Boolean))
    if (rec.tags.replica_of && rec.tags.replica_source_endpoint) hosts.add(rec.tags.replica_source_endpoint)
    return hosts
  }
  _hostRefusal(host, endpoint) {
    return `This secret's host is ${host}. Set it to ${endpoint || 'the endpoint shown'} (for Aurora, the cluster endpoint), or remove host from the secret.`
  }
  // Paged like /api/audit: newest first, the `from` lower bound and `action` applied.
  _auditPage(rows, q) {
    let out = [...rows].reverse()
    if (q.from) out = out.filter(r => r.ts >= q.from)
    if (q.action) out = out.filter(r => r.action === q.action)
    return { rows: clone(out), has_more: false, next_before: null, total: out.length, scanned_to: null }
  }
  _onboardable(id, spaceId) {
    const rec = this.instances.get(id)
    if (!rec || !(rec.member_space_ids.includes(spaceId) || rec.candidate_space_ids.includes(spaceId))) throw new HttpError(404, 'Instance not found')
    if (rec.unsupported_reason) throw new HttpError(400, rec.unsupported_reason)
    if (rec.status === 'deleting') throw new HttpError(409, 'This database is being removed. Wait until it is gone, then run discovery again.')
    if (rec.status === 'delete_failed') throw new HttpError(409, 'Removing this database did not finish. Retry the removal first.')
    return rec
  }

  // ── the HTTP surface ──────────────────────────────────────────────────────
  // Returns { status, body }. Never throws.
  handle({ method, url, headers = {}, body = null, app = '' }) {
    this.tick()
    const u = new URL(url)
    const path = u.pathname
    const q = Object.fromEntries(u.searchParams)
    const c = this._caller(headers)
    let res
    const route = ROUTES.find(([m, re]) => m === method && re.test(path))
    if (!route) {
      this.unhandled.push(`${method} ${path}`)
      res = { status: 501, body: { detail: `golden fake: ${method} ${path} is not implemented` } }
    } else if (!c) {
      res = { status: 401, body: { detail: 'Not authenticated' } }
    } else {
      try {
        const out = route[2].call(this, { c, q, body: body || {}, m: path.match(route[1]) })
        res = { status: 200, body: out }
      } catch (e) {
        if (!(e instanceof HttpError)) throw e
        res = { status: e.status, body: { detail: e.detail } }
      }
    }
    if (res.status >= 400 || res.body?.ok === false) {
      this.failures.push({ method, path, status: res.status, detail: res.body?.detail || res.body?.error || '', stage: res.body?.stage || '', user: c?.user?.key || '-', app })
    }
    this.requests.push({ method, path, search: u.search, user: c?.user?.key || '-', app, status: res.status, body })
    return res
  }

  // Requests made since `mark` (an index into this.requests).
  since(mark) { return this.requests.slice(mark) }
}

// ── route handlers ──────────────────────────────────────────────────────────
// Each is called with this = the FakeBackend and { c, q, body, m }.
const H = {
  listSpaces({ c }) {
    const all = [...this.spaces.values()]
    // The admin console lists the spaces the caller MANAGES: their own, or every space for
    // a super admin (break-glass, via _canManage). Being an operator in another admin's
    // space must never surface it here (rule 2: operators do not use the console; rule 19:
    // an admin never sees another admin's databases or names). Mirrors app.py list_spaces.
    const mine = all.filter(s => this._canManage(c, s))
    return mine.map(s => this._publicSpace(s, c))
  },
  createSpace({ c, body }) {
    if (!this._isAdmin(c)) throw new HttpError(403, 'Admin access required')
    if (!(body.name || '').trim()) throw new HttpError(400, 'Name is required')
    const id = this.newId()
    const s = this._spaceRecord(id, c.user, { name: body.name.trim(), description: body.description || '', tags: body.tags || {} })
    this.spaces.set(id, s)
    // Creating a space never makes anyone an operator of it (rule 5): operators stays empty.
    this._audit(c, 'space_create', { space: s, target: s, after: { name: s.name, primary_account_id: s.primary_account_id } })
    return this._publicSpace(s, c)
  },
  getSpace({ c, m }) { return this._publicSpace(this._space(m[1], c), c) },
  updateSpace({ c, m, body }) {
    const s = this._space(m[1], c, { manage: true })
    const before = { regions: [...s.regions] }
    for (const k of ['regions', 'name', 'description', 'secondary_account_ids', 'secondary_role_names']) if (k in body) s[k] = clone(body[k])
    if ('regions' in body && JSON.stringify(before.regions) !== JSON.stringify(s.regions)) s.access_configured = false
    s.updated_at = now()
    this._audit(c, 'space_update', { space: s, target: s, before, after: { regions: s.regions } })
    return this._publicSpace(s, c)
  },
  deleteSpace({ c, m }) {
    const s = this._space(m[1], c, { manage: true })
    if (s.status === 'deleting') return { message: 'Deleting', status: 'deleting', cleanup: [] }
    s.status = 'deleting'
    this._audit(c, 'space_delete', { space: s, target: s, before: { name: s.name } })
    this._later(this.delays.spaceTeardown, () => {
      for (const rec of [...this.instances.values()]) {
        rec.candidate_space_ids = rec.candidate_space_ids.filter(x => x !== s.space_id)
        if (rec.member_space_ids.includes(s.space_id)) {
          rec.member_space_ids = rec.member_space_ids.filter(x => x !== s.space_id)
          if (!rec.member_space_ids.length) this.instances.delete(rec.instance_id)
        }
      }
      this.spaces.delete(s.space_id)
      this._audit(null, 'space_delete_teardown_complete', { space: s, target: s, after: { result: 'complete' } })
    })
    return { message: 'Deleting', status: 'deleting', cleanup: [] }
  },
  agentRolePolicy() {
    return { role_name: 'dbops-agent', permissions_policy: { Version: '2012-10-17', Statement: [{ Effect: 'Allow', Action: ['rds:DescribeDBInstances'], Resource: '*' }] } }
  },
  configureAccess({ c, m }) {
    const s = this._space(m[1], c, { manage: true })
    if (!s.regions.length) throw new HttpError(400, 'Choose at least one region first.')
    s.access_configured = true
    s.updated_at = now()
    this._audit(c, 'space_access_configure', { space: s, target: s })
    return { status: 'configured', errors: [] }
  },
  // POST /accounts/check (check_space_account and _check_member_account): read-only. The
  // AssumeRole succeeds when the role exists there, its trust accepts the owner admin as
  // ExternalId, and its name is under `<prefix>-*` (all the deployment's own policy lets it
  // assume). AWS gives the same AccessDenied for a missing role and a refusing trust, so a
  // far-side refusal is `denied` only when another of the owner's spaces validated that
  // role in that account; otherwise `missing`.
  accountCheck({ c, m, body }) {
    const s = this._space(m[1], c, { manage: true })
    const account = String(body.account_id || '').trim()
    if (!/^\d{12}$/.test(account)) throw new HttpError(400, 'Account IDs are 12 digits.')
    const owner = s.owner_admin
    const roleName = String(body.role_name || '').trim() || this.agentRoleName(owner)
    if (!/^[\w+=,.@-]{1,64}$/.test(roleName)) throw new HttpError(400, 'Role names are 1 to 64 letters, digits or the characters + = , . @ _ -')
    const others = [...this.spaces.values()].filter(sp => sp.space_id !== s.space_id && sp.owner_admin === owner && sp.secondary_account_ids.includes(account))
    const names = owner === c.sub ? others.map(sp => sp.name || sp.space_id).sort() : []
    const validatedIn = others.filter(sp => sp.account_statuses?.[account] === 'valid' && (sp.secondary_role_names?.[account] || this.agentRoleName(owner)) === roleName)
    const out = { account_id: account, role_name: roleName, spaces: names, policy_version: MEMBER_POLICY_VERSION, role_policy_version: null }
    const prefix = `${PREFIX}-`
    const role = this.memberAccounts.get(account)?.get(roleName)
    if (!roleName.startsWith(prefix)) {
      return { ...out, state: 'denied', fix: 'name',
        message: `This deployment may not assume ${roleName}: its role can assume only roles named ${prefix}*. Use a name that starts with ${prefix}.` }
    }
    if (!role || !role.trusts.has(owner)) {
      if (validatedIn.length) {
        const where = owner === c.sub ? validatedIn[0].name : ''
        return { ...out, state: 'denied', fix: 'trust',
          message: `${roleName} is in account ${account}${where ? ` (your space ${where} validated it)` : ''}, but its trust policy no longer accepts this deployment. ` +
            `In that account, re-apply the trust policy with the command below: it trusts roles in account ${s.primary_account_id} with sts:ExternalId ${owner}.` }
      }
      return { ...out, state: 'missing', message: `No role this deployment can use was found in account ${account}.` }
    }
    out.role_policy_version = role.version || null
    if (role.version === MEMBER_POLICY_VERSION) return { ...out, state: 'ready', message: `The access role ${roleName} is already in account ${account}. Nothing to run.` }
    return { ...out, state: 'outdated', message: `The access role ${roleName} is in account ${account}, but its permissions are older than version ${MEMBER_POLICY_VERSION}.` }
  },
  discover({ c, m }) {
    const s = this._space(m[1], c, { manage: true })
    if (!s.access_configured) throw new HttpError(400, 'Validate access first.')
    // The real route runs discovery in the background and returns at once; the console
    // polls /discovery-status. The fake does the work synchronously, then records a done
    // status for the poll to read.
    const out = this.discoverFor(s.space_id)
    this._audit(c, 'space_discover', { space: s, target: s, after: { regions: s.regions, found: out.total_discovered } })
    s.discovery_status = {
      state: 'done', started_at: now(), finished_at: now(),
      total_discovered: out.total_discovered, new_added: out.new_added,
      proposed_existing: out.proposed_existing, stale_removed: out.stale_removed,
      regions_scanned: out.regions_scanned, accounts_scanned: out.accounts_scanned,
      scan_log: out.scan_log,
    }
    return { status: 'running', discovery_status: { state: 'running', started_at: now() } }
  },
  discoveryStatus({ c, m }) {
    const s = this._space(m[1], c, { manage: true })
    return { space_id: s.space_id, discovery_status: s.discovery_status || { state: 'idle' } }
  },
  candidates({ c, m }) {
    const s = this._space(m[1], c, { manage: true })
    return [...this.instances.values()].filter(r => r.candidate_space_ids.includes(s.space_id)).map(r => this._candidateView(r, s.space_id))
  },
  dismissCandidate({ c, m }) {
    const s = this._space(m[1], c, { manage: true })
    const rec = this.instances.get(m[2])
    if (!rec || !rec.candidate_space_ids.includes(s.space_id)) throw new HttpError(404, 'Instance not found')
    rec.candidate_space_ids = rec.candidate_space_ids.filter(x => x !== s.space_id)
    this._audit(c, 'space_candidate_dismiss', { space: s, target: rec })
    return { status: 'dismissed' }
  },
  databases({ c, q }) {
    const s = this._space(q.space_id || '', c)
    return [...this.instances.values()].filter(r => r.member_space_ids.includes(s.space_id)).map(r => this._memberView(r, s.space_id))
  },
  testCredentials({ c, m, body }) {
    const s = this._space(m[1], c, { manage: true })
    const rec = this._onboardable(m[2], s.space_id)
    const arn = (body.secret_arn || '').trim()
    if (!arn.startsWith('arn:') || !arn.includes(':secretsmanager:')) return { ok: false, stage: 'input', error: 'Provide a valid Secrets Manager ARN.' }
    const refusal = this._namespaceRefusal(s, arn)
    if (refusal) return { ok: false, stage: 'secret_namespace', error: refusal, expected_prefix: this.secretFolder(s) }
    const sec = this.secrets.get(arn)
    if (!sec) {
      return { ok: false, stage: 'fetch_secret', error: `Access denied reading this secret through this space's role in account ${rec.account_id}. ` +
        `Check that the role's permission policy is the one the console shows for this space (its secret read is scoped to '${this.secretFolder(s)}').` }
    }
    const allowed = this._allowedHosts(rec)
    if (sec.host && !allowed.has(sec.host)) return { ok: false, stage: 'host_mismatch', error: this._hostRefusal(sec.host, rec.endpoint) }
    return { ok: true, stage: 'secret_validated', host: rec.endpoint || sec.host, port: rec.port, database: body.database_name || 'postgres',
      user: sec.username, warning: '', note: 'Secret validated. Network connectivity will be verified during Configure.' }
  },
  configure({ c, m, body }) {
    const s = this._space(m[1], c, { manage: true })
    if (s.status === 'deleting') throw new HttpError(409, 'This space is being deleted.')
    const rec = this._onboardable(m[2], s.space_id)
    const arn = (body?.secret_arn || '').trim()
    if (!arn) {
      if (!rec.member_space_ids.includes(s.space_id)) throw new HttpError(400, 'secret_arn is required to onboard this database into this space. Open the Onboard wizard, paste the read-only user\'s Secrets Manager ARN, and run Test before onboarding.')
      return { status: 'configuring', message: 'Network configuration running in background.', endpoint: rec.endpoint, account_id: rec.account_id }
    }
    const refusal = this._namespaceRefusal(s, arn)
    if (refusal) throw new HttpError(400, refusal)
    const sec = this.secrets.get(arn)
    if (sec?.host && !this._allowedHosts(rec).has(sec.host)) throw new HttpError(400, this._hostRefusal(sec.host, rec.endpoint))
    rec.status = 'configuring'
    rec.secret_arn = arn
    rec.config_log = ['Database enriched', 'Creating VPC peering']
    this._later(this.delays.configure, () => {
      if (!this.instances.has(rec.instance_id) || !this.spaces.has(s.space_id)) return
      rec.status = 'configured'
      rec.candidate_space_ids = rec.candidate_space_ids.filter(x => x !== s.space_id)
      if (!rec.member_space_ids.includes(s.space_id)) rec.member_space_ids.push(s.space_id)
      rec.reach_space_id = rec.reach_space_id || s.space_id
      rec.config_log.push('Peering active, routes and security group rule in place', 'Connected as dbops_ro (verify-full)')
      this._refreshCount(s.space_id)
      this._audit(c, 'space_db_onboard', { space: s, target: rec, after: { mode: 'first_onboard', result: 'configured', member_count: rec.member_space_ids.length } })
    })
    return { status: 'configuring', message: 'Database enriched. Network configuration running in background.', endpoint: rec.endpoint, account_id: rec.account_id }
  },
  deleteDatabase({ c, m, q }) {
    const s = this._space(q.space_id || '', c, { manage: true })
    const rec = this.instances.get(m[1])
    if (!rec || !rec.member_space_ids.includes(s.space_id)) throw new HttpError(404, 'Instance not found')
    if (rec.status === 'deleting') return { message: 'Deleting', status: 'deleting', cleanup: [] }
    this._audit(c, 'database_space_remove', { space: s, target: rec, after: { member_count: rec.member_space_ids.length - 1 } })
    if (rec.member_space_ids.length > 1) {
      rec.member_space_ids = rec.member_space_ids.filter(x => x !== s.space_id)
      this._refreshCount(s.space_id)
      return { message: 'Removed from this space', status: 'removed', cleanup: [] }
    }
    rec.status = 'deleting'
    rec.config_error = ''
    this._later(this.delays.teardown, () => {
      if (rec.fail_teardown > 0) {
        rec.fail_teardown--
        rec.status = 'delete_failed'
        rec.config_error = 'The network teardown did not finish. Retry finishes it; the steps below say what is left.'
        rec.config_log = ['Deleted route to pcx-0abc in rtb-0def', 'Teardown stopped at VPC peering pcx-0abc: it is still active']
        this._audit(null, 'database_teardown_failed', { space: s, target: rec, after: { result: 'incomplete' } })
        return
      }
      this.instances.delete(rec.instance_id)
      this._refreshCount(s.space_id)
      this._audit(null, 'database_teardown_complete', { space: s, target: rec, after: { result: 'complete' } })
    })
    return { message: 'Deleting', status: 'deleting', cleanup: [] }
  },
  routeRepair({ c, m }) {
    const rec = this.instances.get(m[1])
    if (!rec) throw new HttpError(404, 'Instance not found')
    const s = this.spaces.get(rec.reach_space_id)
    if (!this._canManage(c, s)) throw new HttpError(403, 'Admin access required')
    rec.route_repair_needed = null
    this._audit(c, 'network_host_routes_repaired', { space: s, target: rec, after: { added: 1, removed: 0 } })
    return { message: 'Host routes repaired: 1 added, 0 removed.' }
  },
  topology({ c, m }) {
    const s = this._space(m[1], c)
    const dbs = [...this.instances.values()].filter(r => r.member_space_ids.includes(s.space_id) && r.status === 'configured')
    const n = dbs.length
    // One database VPC peered with the agent VPC, as build_topology draws it.
    const VPC = 'vpc-0db0000000000001'
    const nodes = n ? [
      { id: 'agent-vpc', type: 'agent_vpc', label: 'Agent VPC', metadata: { vpc_id: 'vpc-0a00000000000001', cidr: '10.90.0.0/16' } },
      { id: VPC, type: 'vpc', label: VPC, account_id: DEPLOYMENT_ACCOUNT, region: REGION, metadata: { vpc_id: VPC.slice(4), cidr: '10.20.0.0/16' } },
      ...dbs.map(r => ({ id: `db-node-${r.instance_id}`, type: 'database', label: r.display_name, account_id: r.account_id, region: r.region,
        health: r.last_health_score, metadata: { vpc_id: VPC.slice(4), engine: r.engine, engine_version: '16.4', instance_class: 'db.r6g.large', status: 'available',
          reachability: 'reachable', reach_method: 'peering', endpoint: r.endpoint, port: r.port, account_id: r.account_id, region: r.region,
          is_cluster: r.cluster_member_roles.length > 0, cluster_member_roles: r.cluster_member_roles, metadata_refreshed_at: now() } })),
    ] : []
    const edges = n ? [{ source: 'agent-vpc', target: VPC, type: 'peering', metadata: { status: 'active', pcx_id: 'pcx-0abc0000000001' } }] : []
    return { generated_at: now(), stats: { databases: n, vpcs: n ? 1 : 0, peering_connections: n ? 1 : 0, lattice_connections: 0, same_vpc_connections: 0 },
      coverage: { databases: n, reachable: n, pending: 0, blocked: 0, unknown: 0, refreshed: n, with_metadata: n }, nodes, edges, warnings: [] }
  },
  topologyRefresh({ c, m }) { this._space(m[1], c); return { status: 'refreshing' } },
  spaceUsers({ c, m }) {
    const s = this._space(m[1], c)
    return { users: [...s.operators].map(sub => this.users.get(sub)).map(u => ({ user_id: u.sub, name: u.name, email: u.email, status: 'CONFIRMED', email_verification: 'verified' })) }
  },
  assignableUsers({ c, m }) {
    this._space(m[1], c, { manage: true })
    const ROLE = { admins: 'admins', 'space-admins': 'space-admins', operators: 'operators' }
    return [...this.users.values()].map(u => ({ user_id: u.sub, name: u.name, email: u.email, role: ROLE[u.role] || 'operators' }))
  },
  addSpaceUser({ c, m, body }) {
    const s = this._space(m[1], c, { manage: true })
    const u = this.users.get(body.principal_id) || [...this.users.values()].find(x => x.email === body.email)
    if (!u) throw new HttpError(404, 'User not found')
    s.operators.add(u.sub)
    this._audit(c, 'space_user_assign', { space: s, target: u, after: { auto_created: false } })
    return { status: 'assigned', user_id: u.sub }
  },
  removeSpaceUser({ c, m }) {
    const s = this._space(m[1], c, { manage: true })
    const u = this.users.get(decodeURIComponent(m[2]))
    if (!u || !s.operators.has(u.sub)) throw new HttpError(404, 'Not assigned')
    s.operators.delete(u.sub)
    this._audit(c, 'space_user_remove', { space: s, target: u })
    return { status: 'removed' }
  },
  spaceAudit({ c, m, q }) {
    const s = this.spaces.get(m[1])
    if (!s || !this._canManage(c, s)) throw new HttpError(403, 'Only the space owner or a super admin can read its audit log')
    return this._auditPage(this.audit.filter(r => r.space_id === s.space_id), q, c)
  },
  globalAudit({ c, q }) {
    if (!this._isSuper(c)) throw new HttpError(403, 'Admin access required')
    return this._auditPage(this.audit, q, c)
  },
  auditMeta() {
    return { actions: Object.entries(ACTIONS).map(([code, [label, target_type]]) => ({ code, label, target_type })),
      vault_enabled: this.auditSettings.vault_enabled, effective_retention_days: this.auditSettings.effective_retention_days }
  },
  getAuditSettings({ c }) {
    if (!this._isSuper(c)) throw new HttpError(403, 'Admin access required')
    return clone(this.auditSettings)
  },
  putAuditSettings({ c, body }) {
    if (!this._isSuper(c)) throw new HttpError(403, 'Admin access required')
    if (body.vault_enabled === false && this.auditSettings.vault_enabled) {
      if ((body.confirm || '').trim().toLowerCase() !== VAULT_OFF_PHRASE) throw new HttpError(400, `Type '${VAULT_OFF_PHRASE}' to turn the vault off.`)
      // Recorded in the vault first, then switched (rule 24).
      this._audit(c, 'audit_vault_set', { before: { vault_enabled: true }, after: { vault_enabled: false } })
      this.auditSettings.vault_enabled = false
    } else if (body.vault_enabled === true && !this.auditSettings.vault_enabled) {
      this.auditSettings.vault_enabled = true
      this._audit(c, 'audit_vault_set', { before: { vault_enabled: false }, after: { vault_enabled: true } })
    }
    if (body.retention_days) this.auditSettings.retention_days = this.auditSettings.effective_retention_days = body.retention_days
    Object.assign(this.auditSettings, { configured: true, updated_at: now(), updated_by_email: c.email })
    return clone(this.auditSettings)
  },
  cognitoUsers({ c }) {
    if (!this._isSuper(c)) throw new HttpError(403, 'Admin access required')
    return [...this.users.values()].map(u => ({ username: u.sub, user_id: u.sub, email: u.email, name: u.name, role: u.role,
      status: 'CONFIRMED', enabled: true, email_verification: 'verified',
      spaces: [...this.spaces.values()].filter(s => s.operators.has(u.sub)).map(s => ({ space_id: s.space_id, name: s.name })) }))
  },
  mySpaces({ c }) {
    // The operator group in the token, and nothing else (get_my_spaces).
    return { spaces: [...this.spaces.values()].filter(s => c.groups.includes(`space-${s.space_id}`)).map(s => ({ space_id: s.space_id, name: s.name })) }
  },
  mySpaceDatabases({ c, m }) {
    const s = this.spaces.get(m[1])
    if (!s || !c.groups.includes(`space-${s.space_id}`)) throw new HttpError(403, 'Access to this space is required')
    return [...this.instances.values()].filter(r => r.member_space_ids.includes(s.space_id)).map(r => this._operatorView(r, s.space_id))
  },
  databaseMembers({ c, m }) {
    const rec = this.instances.get(decodeURIComponent(m[1]))
    if (!rec) throw new HttpError(404, 'Instance not found')
    if (!rec.member_space_ids.some(sid => this._canView(c, this.spaces.get(sid)))) throw new HttpError(403, 'Access to this space is required')
    const cluster = rec.cluster_member_roles.length > 0
    const roles = cluster ? rec.cluster_member_roles : [{ instance_id: rec.tags.raw_identifier, is_writer: true, endpoint: rec.endpoint }]
    const members = roles.map(r => ({ instance_id: r.instance_id, role: r.is_writer ? 'writer' : 'reader', availability_zone: r.availability_zone || 'us-east-1a',
      endpoint: r.endpoint, health: 'ok', replica_lag_ms: r.is_writer ? null : (r.replica_lag_ms ?? null),
      tier1_state: r.skip_reason ? 'skipped' : 'fresh', last_tier1_at: now(), ...(r.skip_reason ? { skip_reason: r.skip_reason } : {}) }))
    return { instance_id: rec.instance_id, is_cluster: cluster,
      endpoints: { cluster: cluster ? rec.endpoint : '', reader: cluster ? rec.reader_endpoint : '', instance: Object.fromEntries(members.map(x => [x.instance_id, x.endpoint])) },
      writer_instance_id: members.find(x => x.role === 'writer')?.instance_id || null, members_changed_at: rec.members_changed_at || null,
      members, reader_skip_reason: '', store_configured: true }
  },
  monitoringStatus({ c }) {
    if (!this._isAdmin(c)) throw new HttpError(403, 'Admin access required')
    const dbs = [...this.instances.values()].filter(r => r.status === 'configured' && r.member_space_ids.some(sid => this._canView(c, this.spaces.get(sid))))
    return { databases: dbs.map(r => ({ ...this._operatorView(r, r.member_space_ids[0]) })), scope: this._isSuper(c) ? 'account' : 'admin', store_configured: true }
  },
  // Operator app chrome the scenarios do not exercise: answered empty, in the real shapes.
  sessions() { return { sessions: [] } },
  investigations() { return { investigations: [] } },
  spaceModels() { return { models: [], default_model: '', enabled: true } },
  recommendations() { return { recommendations: [] } },
  fleet() { return { rows: [], fleet: [], total: 0 } },
  scanJobs() { return { jobs: [] } },
  activeScans() { return { active: [] } },
  scanMetricsBucketed() { return { rows: [], bucket_seconds: 300, hours: 24 } },
  metricsSeries() { return { databases: [], days: 7 } },
  scanMetrics() { return { rows: [], total: 0, next_before: null, has_more: false } },
  scanResults() { return { rows: [], total: 0, next_before: null, has_more: false } },
  knowledgeBuiltin() { return { skills: [], knowledge_documents: [] } },
  spaceKnowledge({ c, m }) { this._space(m[1], c); return { custom: [], inherited: [], disabled_items: [] } },
  memory() { return { records: [] } },
  emailSender() { return { ready: true, configured: true, sender: 'no-reply@example.com' } },
  posture() { return { controls: [], manage: null } },
  budget() { return { configured: false } },
  rateLimit() { return { configured: false, per_minute: null } },
  guardrailAlarms() { return { configured: false } },
}


const ROUTES = [
  ['GET', /^\/api\/spaces$/, H.listSpaces],
  ['POST', /^\/api\/spaces$/, H.createSpace],
  ['GET', /^\/api\/agent-role-policy$/, H.agentRolePolicy],
  ['GET', /^\/api\/spaces\/([^/]+)$/, H.getSpace],
  ['PUT', /^\/api\/spaces\/([^/]+)$/, H.updateSpace],
  ['DELETE', /^\/api\/spaces\/([^/]+)$/, H.deleteSpace],
  ['POST', /^\/api\/spaces\/([^/]+)\/configure-access$/, H.configureAccess],
  ['POST', /^\/api\/spaces\/([^/]+)\/accounts\/check$/, H.accountCheck],
  ['POST', /^\/api\/spaces\/([^/]+)\/discover$/, H.discover],
  ['GET', /^\/api\/spaces\/([^/]+)\/discovery-status$/, H.discoveryStatus],
  ['GET', /^\/api\/spaces\/([^/]+)\/candidates$/, H.candidates],
  ['DELETE', /^\/api\/spaces\/([^/]+)\/candidates\/([^/]+)$/, H.dismissCandidate],
  ['POST', /^\/api\/spaces\/([^/]+)\/databases\/([^/]+)\/test-credentials$/, H.testCredentials],
  ['POST', /^\/api\/spaces\/([^/]+)\/databases\/([^/]+)\/configure$/, H.configure],
  ['GET', /^\/api\/spaces\/([^/]+)\/topology$/, H.topology],
  ['POST', /^\/api\/spaces\/([^/]+)\/topology\/refresh$/, H.topologyRefresh],
  ['GET', /^\/api\/spaces\/([^/]+)\/users$/, H.spaceUsers],
  ['POST', /^\/api\/spaces\/([^/]+)\/users$/, H.addSpaceUser],
  ['DELETE', /^\/api\/spaces\/([^/]+)\/users\/([^/]+)$/, H.removeSpaceUser],
  ['GET', /^\/api\/spaces\/([^/]+)\/assignable-users$/, H.assignableUsers],
  ['GET', /^\/api\/spaces\/([^/]+)\/audit$/, H.spaceAudit],
  ['GET', /^\/api\/spaces\/([^/]+)\/investigations$/, H.investigations],
  ['GET', /^\/api\/spaces\/([^/]+)\/models$/, H.spaceModels],
  ['GET', /^\/api\/spaces\/([^/]+)\/knowledge\/custom$/, H.spaceKnowledge],
  ['GET', /^\/api\/spaces\/([^/]+)\/recommendations$/, H.recommendations],
  ['GET', /^\/api\/databases$/, H.databases],
  ['DELETE', /^\/api\/databases\/([^/]+)$/, H.deleteDatabase],
  ['POST', /^\/api\/databases\/([^/]+)\/route-repair$/, H.routeRepair],
  ['GET', /^\/api\/database-members\/([^/]+)$/, H.databaseMembers],
  ['GET', /^\/api\/cognito\/users$/, H.cognitoUsers],
  ['GET', /^\/api\/my-spaces$/, H.mySpaces],
  ['GET', /^\/api\/my-spaces\/([^/]+)\/databases$/, H.mySpaceDatabases],
  ['GET', /^\/api\/audit$/, H.globalAudit],
  ['GET', /^\/api\/audit\/meta$/, H.auditMeta],
  ['GET', /^\/api\/audit\/settings$/, H.getAuditSettings],
  ['PUT', /^\/api\/audit\/settings$/, H.putAuditSettings],
  ['GET', /^\/api\/admin\/monitoring-status$/, H.monitoringStatus],
  ['GET', /^\/api\/sessions$/, H.sessions],
  ['GET', /^\/api\/sessions\/([^/]+)\/investigations$/, H.investigations],
  ['GET', /^\/api\/scan-metrics\/fleet$/, H.fleet],
  ['GET', /^\/api\/scan-metrics\/([^/]+)\/bucketed$/, H.scanMetricsBucketed],
  ['GET', /^\/api\/scan-metrics\/([^/]+)$/, H.scanMetrics],
  ['GET', /^\/api\/spaces\/([^/]+)\/active-scans$/, H.activeScans],
  ['GET', /^\/api\/spaces\/([^/]+)\/metrics-series$/, H.metricsSeries],
  ['GET', /^\/api\/scan-results$/, H.scanResults],
  ['GET', /^\/api\/scan\/([^/]+)\/jobs$/, H.scanJobs],
  ['GET', /^\/api\/knowledge\/builtin$/, H.knowledgeBuiltin],
  ['GET', /^\/api\/memory$/, H.memory],
  ['GET', /^\/api\/email-sender$/, H.emailSender],
  ['GET', /^\/api\/security-posture$/, H.posture],
  ['GET', /^\/api\/settings\/budget$/, H.budget],
  ['GET', /^\/api\/settings\/rate-limit$/, H.rateLimit],
  ['GET', /^\/api\/settings\/guardrail-alarms$/, H.guardrailAlarms],
]
