// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
//
// S3 tenancy (rules 2 to 5, 19):
//   * admin B's console lists only the spaces they manage; being an operator in admin A's
//     space does not put it there, and the backend refuses B's manage calls to it;
//   * a super admin can view and manage any space, and the audit log flags the action as
//     taken on another admin's space;
//   * an operator cannot open the admin console;
//   * the operator app lists only the spaces a user was added to; a creator who did not
//     add themselves sees none.
import { standardUsers } from '../lib/harness.mjs'
import { isVisible, openStage } from '../lib/ui.mjs'
import { rule3 } from '../rules.mjs'

export default {
  id: 'S3',
  name: 'tenancy',
  async run(h) {
    const fake = h.fake
    standardUsers(fake)
    const payments = fake.seedSpace('admin-a', { name: 'Payments', operators: ['admin-b', 'op-1'] })
    const ledger = fake.seedSpace('admin-b', { name: 'Ledger' })
    const analytics = fake.seedSpace('admin-a', { name: 'Analytics', operators: ['op-1'] })
    h.intendOperator(payments, 'admin-b')
    h.intendOperator(payments, 'op-1')
    h.intendOperator(analytics, 'op-1')
    fake.seedMember('orders-db', [payments])
    fake.seedMember('billing-cluster', [ledger])

    const b = await h.open('admin', 'admin-b')
    await h.step('admin B\'s console lists only their own space, not one they are an operator in', b, async () => {
      const rows = b.page.locator('tbody tr')
      await rows.first().waitFor()
      const names = (await rows.allInnerTexts()).map(t => t.split('\n')[0].trim()).sort()
      // Payments is admin A's; admin B is only an operator in it. Being an operator never
      // puts a space in the admin console (rule 2: operators do not use the console; rule
      // 19: an admin never sees another admin's databases or names). It used to leak in
      // only because admin B is also a space admin, so an operator-grant landed in their
      // accessible set; the console now lists the spaces the caller manages, so it is gone.
      h.expect(JSON.stringify(names) === JSON.stringify(['Ledger']), `admin B sees ${JSON.stringify(names)}`, 19)
    }, { checkpoint: 'admin-b-list' })

    // The console gives admin B no way to open Payments. The backend refuses their manage
    // calls to it anyway, made straight to the API (no page involved, so no rule check
    // applies to it).
    await h.step('the fake refuses admin B\'s manage calls like the backend', null, async () => {
      for (const [method, p] of [['POST', `/api/spaces/${payments}/discover`], ['DELETE', `/api/spaces/${payments}`], ['POST', `/api/spaces/${payments}/users`]]) {
        const r = fake.handle({ method, url: `http://localhost:8084${p}`, headers: { authorization: `Bearer ${fake.tokenFor('admin-b')}` }, body: {} })
        h.expect(r.status === 403, `${method} ${p} by a non-owner answered ${r.status}`, 3)
      }
      fake.failures.splice(-3)
      fake.requests.splice(-3)
    })

    const s = await h.open('admin', 'super')
    await h.step('a super admin manages admin A\'s space (break-glass)', s, async () => {
      await s.page.getByRole('button', { name: 'Payments', exact: true }).click()
      // Lands on Overview like any set-up space; the manage controls live in the stages.
      await s.page.getByRole('heading', { name: 'Needs attention' }).waitFor()
      await openStage(s.page, 'Databases')
      await s.page.getByText('Onboarded databases (1)').waitFor()
      h.expect(!(await isVisible(s.page.getByText('Read-only.'))), 'a super admin gets the read-only notice', 4)
      await s.page.getByRole('button', { name: /^Run discovery again$/ }).click()
      await s.page.getByText(/Found, not yet onboarded/).waitFor()
    })

    // Guards the guard: the rule-3 check must see enabled manage controls on a page that
    // has them, or its passes above mean nothing.
    await h.step('self-check: the rule 3 check sees enabled manage controls', null, async () => {
      const v = await rule3(s, { space: payments, canManage: false }, [], [])
      h.expect(v.some(x => /can press "Run discovery again"/.test(x.message)), `the rule-3 check is blind: ${JSON.stringify(v)}`)
    })

    await h.step('the audit flags the super admin\'s action on another admin\'s space', s, async () => {
      await s.page.getByRole('button', { name: /Audit log/ }).click()
      const row = s.page.getByRole('region', { name: 'Audit log' }).locator('tbody tr', { hasText: 'Ran discovery' })
      await row.waitFor()
      await row.getByText("On another admin's space").waitFor()
      await row.getByText('Super admin').waitFor()
    }, { checkpoint: 'super-audit' })

    const opAdmin = await h.open('admin', 'op-1')
    await h.step('an operator cannot open the admin console', opAdmin, async () => {
      await opAdmin.page.getByText('Access Denied').waitFor()
    }, { checkpoint: 'operator-admin-denied' })

    const op = await h.open('operator', 'op-1')
    await h.step('the operator app lists only the spaces the operator was added to', op, async () => {
      await op.page.getByText('Select a Space').waitFor()
      const names = (await op.page.locator('a .font-medium').allInnerTexts()).map(t => t.trim()).sort()
      h.expect(JSON.stringify(names) === JSON.stringify(['Analytics', 'Payments']), `the operator app lists ${JSON.stringify(names)}`, 5)
    }, { checkpoint: 'operator-space-picker', region: '#root' })

    const creator = await h.open('operator', 'admin-a')
    await h.step('a creator who did not add themselves has no space in the operator app', creator, async () => {
      await creator.page.getByText('No spaces to open').waitFor()
    }, { checkpoint: 'creator-not-operator', region: '#root' })
  },
}
