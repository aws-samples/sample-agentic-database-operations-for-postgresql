// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
//
// S2 secret rules (rules 8, 9, 17, 18) in the onboarding wizard:
//   * a secret named the old way (outside the owner's folder) is flagged in Step 2 and
//     refused by Test with the plain message that names the secret and the folder;
//   * a secret whose host is another database's endpoint is refused, naming the
//     endpoint to use;
//   * a read replica accepts its primary's secret and onboards with it.
import { standardUsers } from '../lib/harness.mjs'
import { rowNamed, isVisible, wizardTest } from '../lib/ui.mjs'

export default {
  id: 'S2',
  name: 'secret rules',
  async run(h) {
    const fake = h.fake
    standardUsers(fake)
    const spaceId = fake.seedSpace('admin-a', { name: 'Payments' })
    fake.discoverFor(spaceId)
    const space = fake.spaces.get(spaceId)
    const folder = fake.secretFolder(space)
    const orders = fake.instances.get('db-orders-db')
    const replica = fake.instances.get('db-orders-replica')
    // The secret the user reported: created before secrets were kept per admin.
    const oldArn = fake.putSecret('dbops/aurora-postgresql/orders-db/readonly', { host: orders.endpoint })
    const wrongHostArn = fake.putSecret(fake.secretNameFor(space, 'orders-db'), { host: replica.endpoint })
    const primaryArn = fake.putSecret(`${folder}postgres/orders-db-primary/readonly`, { host: orders.endpoint })

    const a = await h.open('admin', 'admin-a')
    const page = a.page
    const found = () => page.getByText(/Found, not yet onboarded/).locator('xpath=following::table[1]')
    const dlg = page.getByRole('dialog', { name: 'Onboard database' })
    const onboardBtn = () => dlg.getByRole('button', { name: /^Onboard$/ })

    await h.step('open the space on Databases', a, async () => {
      await page.getByRole('button', { name: 'Payments', exact: true }).click()
      await page.getByText(/Found, not yet onboarded \(3\)/).waitFor()
    })

    await h.step('Step 1 names the folder and says to keep the name', a, async () => {
      await rowNamed(found(), 'orders-db').getByRole('button', { name: 'Onboard' }).click()
      const note = dlg.getByText('Keep the secret name as shown.').locator('..')
      await note.waitFor()
      h.expect((await dlg.innerText()).includes(folder), `the Step 1 note does not name the folder ${folder}`, 8)
      h.expect((await dlg.innerText()).includes(`--name "${folder}postgres/orders-db/readonly"`), 'the Step 1 command does not create the secret in the folder', 8)
    })

    await h.step('an old-named secret is flagged in Step 2 and refused by Test', a, async () => {
      await dlg.getByRole('button', { name: 'Next', exact: true }).click()
      await dlg.locator('input[placeholder^="arn:aws:secretsmanager"]').fill(oldArn)
      const warn = dlg.getByRole('alert').filter({ hasText: 'This secret will be refused.' })
      await warn.waitFor()
      const wt = await warn.innerText()
      h.expect(wt.includes('dbops/aurora-postgresql/orders-db/readonly') && wt.includes(folder), `the Step 2 warning does not name the secret and the folder: ${wt}`, 8)
      await wizardTest(dlg)
      const result = dlg.getByText(/^✕ /)
      await result.waitFor()
      const rt = await result.innerText()
      h.expect(rt.startsWith("✕ This secret is named 'dbops/aurora-postgresql/orders-db/readonly'"), `Test does not give the plain refusal: ${rt}`, 18)
      h.expect(rt.includes(folder), 'the refusal does not name the folder', 8)
      h.expect(await onboardBtn().isDisabled(), 'Onboard is enabled after a refused Test', 17)
      h.expect(!fake.requests.some(r => r.path.endsWith('/configure')), 'a refused secret reached Configure', 8)
    }, { checkpoint: 'old-name-refused', region: '[role="dialog"]' })

    await h.step('a wrong host is refused, naming the endpoint', a, async () => {
      await dlg.locator('input[placeholder^="arn:aws:secretsmanager"]').fill(wrongHostArn)
      h.expect(!(await isVisible(dlg.getByText('This secret will be refused.'))), 'a correctly named secret is flagged', 8)
      await wizardTest(dlg)
      const result = dlg.getByText(/^✕ /)
      await result.waitFor()
      const rt = await result.innerText()
      h.expect(rt.includes(`This secret's host is ${replica.endpoint}`) && rt.includes(`Set it to ${orders.endpoint}`), `the refusal does not name the endpoint to use: ${rt}`, 9)
      h.expect(await onboardBtn().isDisabled(), 'Onboard is enabled after a refused Test', 17)
    }, { checkpoint: 'wrong-host-refused', region: '[role="dialog"]' })

    await h.step('a replica accepts its primary\'s secret', a, async () => {
      await dlg.getByRole('button', { name: 'Cancel', exact: true }).click()
      await dlg.waitFor({ state: 'detached' })
      const replicaRow = rowNamed(found(), 'orders-replica')
      h.expect((await replicaRow.innerText()).includes('replica of'), 'the replica row is not labelled as a replica of its primary', 9)
      await replicaRow.getByRole('button', { name: 'Onboard' }).click()
      await dlg.getByRole('button', { name: 'Next', exact: true }).click()
      await dlg.locator('input[placeholder^="arn:aws:secretsmanager"]').fill(primaryArn)
      await wizardTest(dlg)
      await dlg.getByText('✓ Secret validated').waitFor()
      h.expect(!(await onboardBtn().isDisabled()), 'Onboard stays disabled for the primary\'s secret', 9)
    }, { checkpoint: 'replica-primary-secret', region: '[role="dialog"]' })

    await h.step('the replica onboards with it', a, async () => {
      await onboardBtn().click()
      await dlg.waitFor({ state: 'detached' })
      await h.eventually(async () => isVisible(page.getByText('Onboarded databases (1)')), { what: 'orders-replica as an onboarded database' })
      const onboarded = page.getByText(/^Onboarded databases/).locator('xpath=following::table[1]')
      await rowNamed(onboarded, 'orders-replica').getByText('Ready').waitFor()
    })
  },
}
