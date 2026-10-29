// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
//
// S5 cluster (rules 15, 20, 21) in the operator app: the members table shows the
// writer and the readers with lag; a reader the network path cannot reach shows its
// reason and is disabled in the member picker; after a failover the roles swap on the
// open page without a refresh (within one Tier 1 interval of the page's own clock), and
// the space's audit log records the failover.
import { standardUsers } from '../lib/harness.mjs'

const UNREACHABLE = 'skipped: not routed (10.0.1.6 has no owned host route)'
const DRAWER = 'div.shadow-sm:has([aria-label="Database records"])'
const TIER1_INTERVAL_MS = 5 * 60 * 1000

export default {
  id: 'S5',
  name: 'cluster',
  async run(h) {
    const fake = h.fake
    standardUsers(fake)
    const payments = fake.seedSpace('admin-a', { name: 'Payments', operators: ['op-1'] })
    h.intendOperator(payments, 'op-1')
    const ep = (id) => `${id}.c0ffee00.us-east-1.rds.amazonaws.com`
    const id = fake.seedMember('billing-cluster', [payments], {
      cluster_member_roles: [
        { instance_id: 'billing-1', is_writer: true, endpoint: ep('billing-1'), availability_zone: 'us-east-1a' },
        { instance_id: 'billing-2', is_writer: false, endpoint: ep('billing-2'), availability_zone: 'us-east-1b', replica_lag_ms: 18 },
        { instance_id: 'billing-3', is_writer: false, endpoint: ep('billing-3'), availability_zone: 'us-east-1c', replica_lag_ms: 2400, skip_reason: UNREACHABLE },
      ],
    })

    const op = await h.open('operator', 'op-1', { path: `/?space_id=${payments}`, clock: true })
    const page = op.page
    const drawer = page.locator(DRAWER)
    const roleOf = async (member) => (await drawer.locator('tbody tr', { hasText: member }).locator('td').first().innerText()).trim()

    await h.step('the members table shows the writer and the readers with lag', op, async () => {
      await page.getByRole('button', { name: 'Dashboard' }).first().click()
      await page.getByRole('button', { name: 'Members', exact: true }).first().click()
      await drawer.getByRole('tab', { name: 'Members' }).waitFor()
      await drawer.locator('tbody tr', { hasText: 'billing-3' }).waitFor()
      h.expect(await roleOf('billing-1') === 'writer', 'billing-1 is not shown as the writer', 20)
      h.expect(await roleOf('billing-2') === 'reader', 'billing-2 is not shown as a reader', 20)
      await drawer.locator('tbody tr', { hasText: 'billing-2' }).getByText('18 ms').waitFor()
      await drawer.getByText(/billing-3: not routed \(10\.0\.1\.6/).waitFor()
    }, { checkpoint: 'members', region: DRAWER })

    await h.step('an unreachable reader is disabled in the picker with its reason', op, async () => {
      await drawer.getByRole('tab', { name: 'Metrics (Tier 1)' }).click()
      const sel = drawer.locator(`select#member-${id}`)
      await sel.waitFor()
      await h.eventually(async () => (await sel.locator('option', { hasText: 'billing-3' }).isDisabled()), { what: 'billing-3 disabled in the picker', rule: 20 })
      const text = (await sel.locator('option', { hasText: 'billing-3' }).innerText()).trim()
      h.expect(text.includes('not routed (10.0.1.6'), `the option does not give the reason: ${text}`, 18)
      h.expect(!(await sel.locator('option', { hasText: 'billing-2' }).isDisabled()), 'the reachable reader is disabled', 20)
    })

    await h.step('a failover swaps the roles on the open page, no refresh', op, async () => {
      await drawer.getByRole('tab', { name: 'Members' }).click()
      await drawer.locator('tbody tr', { hasText: 'billing-1' }).waitFor()
      fake.failover(id)   // what the Tier 1 collector records when RDS reports the new writer
      const reads = () => fake.requests.filter(r => r.path === `/api/database-members/${id}`).length
      const before = reads()
      await page.clock.runFor(TIER1_INTERVAL_MS)
      // Was GH-1 (fixed in 753ae86): the open table re-reads the members on its own.
      await h.eventually(async () => (await roleOf('billing-2')) === 'writer' && (await roleOf('billing-1')) === 'reader',
        { timeout: 4000, what: 'billing-2 as the writer on the open members table', rule: 21 })
      h.expect(reads() > before, 'the roles swapped without the open table reading the members again', 21)
    })

    const a = await h.open('admin', 'admin-a')
    await h.step('the failover is in the space\'s audit log', a, async () => {
      await a.page.getByRole('button', { name: 'Payments', exact: true }).click()
      await a.page.getByRole('button', { name: /Audit log/ }).click()
      const row = a.page.getByRole('region', { name: 'Audit log' }).locator('tbody tr', { hasText: 'Detected cluster failover' })
      await row.waitFor()
      await row.getByText('System', { exact: true }).first().waitFor()
      h.expect(fake.audit.filter(r => r.action === 'cluster_failover_detected').length === 1, 'the failover was recorded more than once', 21)
    }, { checkpoint: 'failover-audit' })
  },
}
