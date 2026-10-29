// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
//
// S4 lifecycle (rules 15 to 17): removing a database and deleting a space answer at
// once, show "Deleting…", and end with the row gone, on their own; a double click sends
// one request; a teardown that fails shows Delete failed with the reason and Retry, and
// Retry finishes it.
import { standardUsers } from '../lib/harness.mjs'
import { rowNamed, isVisible, openStage } from '../lib/ui.mjs'

const deletes = (fake, mark, suffix) => fake.since(mark).filter(r => r.method === 'DELETE' && r.path.endsWith(suffix)).length

export default {
  id: 'S4',
  name: 'lifecycle',
  async run(h) {
    const fake = h.fake
    standardUsers(fake)
    const payments = fake.seedSpace('admin-a', { name: 'Payments' })
    const old = fake.seedSpace('admin-a', { name: 'Old reporting' })
    fake.seedMember('orders-db', [payments])
    fake.seedMember('orders-replica', [payments], { fail_teardown: 1 })
    fake.seedMember('billing-cluster', [old])

    const a = await h.open('admin', 'admin-a')
    const page = a.page
    const onboarded = () => page.getByText(/^Onboarded databases/).locator('xpath=following::table[1]')

    await h.step('open Payments on Databases', a, async () => {
      await page.getByRole('button', { name: 'Payments', exact: true }).click()
      await openStage(page, 'Databases')
      await page.getByText('Onboarded databases (2)').waitFor()
    })

    let mark = fake.requests.length
    await h.step('remove a database with a double click: one request, Deleting…', a, async () => {
      await page.getByRole('button', { name: 'Remove orders-db from this space' }).dblclick()
      const row = rowNamed(onboarded(), 'orders-db')
      await row.getByText('Deleting…').waitFor({ timeout: 2000 })
      await row.getByText('Removing…').waitFor()
      h.expect(deletes(fake, mark, '/api/databases/db-orders-db') === 1, `a double click sent ${deletes(fake, mark, '/api/databases/db-orders-db')} removal requests`, 16)
      h.expect(a.dialogs.length === 1, `a double click asked ${a.dialogs.length} times`, 16)
    }, { checkpoint: 'db-deleting' })

    await h.step('the removed database disappears on its own', a, async () => {
      await h.eventually(async () => isVisible(page.getByText('Onboarded databases (1)')), { what: 'orders-db gone from the table' })
      h.expect(!fake.instances.has('db-orders-db'), 'the backend still holds orders-db')
    })

    await h.step('a failed teardown shows Delete failed, the reason and Retry', a, async () => {
      await page.getByRole('button', { name: 'Remove orders-replica from this space' }).click()
      const row = rowNamed(onboarded(), 'orders-replica')
      await row.getByText('Deleting…').waitFor({ timeout: 2000 })
      await h.eventually(async () => isVisible(row.getByText('Delete failed')), { what: 'Delete failed on orders-replica' })
      await onboarded().getByText('Removal incomplete:').waitFor()
      await onboarded().getByText(/Teardown stopped at VPC peering/).waitFor()
      await row.getByRole('button', { name: 'Retry removing orders-replica' }).waitFor()
    }, { checkpoint: 'db-delete-failed' })

    await h.step('Retry finishes the removal', a, async () => {
      await page.getByRole('button', { name: 'Retry removing orders-replica' }).click()
      await rowNamed(onboarded(), 'orders-replica').getByText('Deleting…').waitFor({ timeout: 2000 })
      await h.eventually(async () => isVisible(page.getByText('Onboarded databases (0)')), { what: 'orders-replica gone after Retry' })
    }, { checkpoint: 'db-retried' })

    await h.step('open the other space', a, async () => {
      await page.getByRole('button', { name: 'Agent Spaces', exact: true }).first().click()
      await page.getByRole('button', { name: 'Old reporting', exact: true }).click()
      await openStage(page, 'Accounts & regions')
      await page.getByText('Delete this Agent Space').waitFor()
    })

    mark = fake.requests.length
    const dialogs = a.dialogs.length
    await h.step('delete the space with a double click: one request, Deleting…', a, async () => {
      await page.getByRole('button', { name: /Delete Agent Space/ }).dblclick()
      const row = page.locator('tbody tr', { hasText: 'Old reporting' })
      await row.getByText('Deleting…').waitFor({ timeout: 3000 })
      h.expect(deletes(fake, mark, `/api/spaces/${old}`) === 1, `a double click sent ${deletes(fake, mark, `/api/spaces/${old}`)} delete requests`, 16)
      h.expect(a.dialogs.length - dialogs === 1, `a double click asked ${a.dialogs.length - dialogs} times`, 16)
    }, { checkpoint: 'space-deleting' })

    await h.step('the deleted space disappears from the list on its own', a, async () => {
      await h.eventually(async () => !(await isVisible(page.locator('tbody tr', { hasText: 'Old reporting' }))), { timeout: 12000, what: 'Old reporting gone from the list' })
      await page.locator('tbody tr', { hasText: 'Payments' }).waitFor()
    }, { checkpoint: 'space-gone' })
  },
}
