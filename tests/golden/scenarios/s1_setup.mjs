// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
//
// S1 setup flow: a space admin creates a space and is NOT made its operator, validates
// access, runs discovery (the Aurora Global Database is listed only under "Not
// supported"), onboards an RDS instance and an Aurora cluster with a reader and watches
// "Configuring…" turn into Ready on its own, walks on with Next (Operators is empty
// until someone is added), reaches Topology and sees the map draw both databases inside
// its visible area without Refresh metadata or a reload, adds an operator, and reads
// every action back in the space's audit log.
import { standardUsers } from '../lib/harness.mjs'
import { rowNamed, openStage, nextTo, boxInMap, isVisible, wizardPaste, wizardTest } from '../lib/ui.mjs'
import { GLOBAL_DB_REASON } from '../fake_api.mjs'

export default {
  id: 'S1',
  name: 'setup flow',
  async run(h) {
    const fake = h.fake
    standardUsers(fake)
    const a = await h.open('admin', 'admin-a')
    const page = a.page
    let space

    await h.step('space list is empty', a, async () => {
      await page.getByText('No Agent Spaces').waitFor()
    }, { checkpoint: 'empty-list' })

    await h.step('create a space', a, async () => {
      await page.getByRole('button', { name: /Create Agent Space/ }).first().click()
      await page.getByPlaceholder('e.g., prod-us, staging, team-payments').fill('Payments')
      await page.getByPlaceholder('Describe the purpose of this Agent Space').fill('Payments databases')
      await page.getByRole('button', { name: 'Create', exact: true }).click()
      await page.getByRole('heading', { name: 'Payments' }).waitFor()
      space = [...fake.spaces.values()][0]
      h.expect(space && space.created_by === a.sub, 'the space was not created by the admin')
      h.expect(space.operators.size === 0, 'creating the space made someone an operator', 5)
      const opsStage = await page.getByRole('navigation', { name: 'Space setup' }).getByRole('button', { name: /Operators/ }).getAttribute('title')
      h.expect(!opsStage || !/have access/.test(opsStage), `the Operators stage claims access right after creation: ${opsStage}`, 5)
    }, { checkpoint: 'created' })

    await h.step('validate access in us-east-1', a, async () => {
      await page.getByRole('button', { name: 'us-east-1', exact: true }).click()
      await page.getByRole('button', { name: /^Validate access$/ }).click()
      await page.getByText(/All accounts validated in 1 region/).waitFor()
      h.expect(fake.spaces.get(space.space_id).access_configured, 'access was not configured in the backend')
    }, { checkpoint: 'access-validated' })

    await h.step('run discovery: the Global Database is only under Not supported', a, async () => {
      await page.getByRole('button', { name: /Next: Databases/ }).click()
      await page.getByRole('button', { name: /^Run discovery$/ }).click()
      await page.getByText(/Found, not yet onboarded \(3\)/).waitFor()
      const found = page.getByText(/Found, not yet onboarded/).locator('xpath=following::table[1]')
      h.expect(!(await isVisible(rowNamed(found, 'ledger-global'))), 'the Aurora Global Database is offered as a candidate', 14)
      await page.getByText('Not supported (1)').click()
      const ns = page.getByRole('table', { name: /cannot be onboarded/ })
      await rowNamed(ns, 'ledger-global').getByText(GLOBAL_DB_REASON).waitFor()
    }, { checkpoint: 'discovered' })

    await h.step('onboard the RDS instance: Configuring… then Ready, no refresh', a, async () => {
      const arn = fake.putSecret(fake.secretNameFor(space, 'orders-db', 'postgres'), { host: fake.instances.get('db-orders-db').endpoint })
      const dlg = await wizardPaste(page, 'orders-db', arn)
      await wizardTest(dlg)
      await dlg.getByText('✓ Secret validated').waitFor()
      await dlg.getByRole('button', { name: /^Onboard$/ }).click()
      await dlg.waitFor({ state: 'detached' })
      const found = page.getByText(/Found, not yet onboarded/).locator('xpath=following::table[1]')
      await rowNamed(found, 'orders-db').getByText('Configuring…').waitFor({ timeout: 3000 })
      await h.eventually(async () => isVisible(page.getByText('Onboarded databases (1)')), { what: 'orders-db as an onboarded database' })
      const onboarded = page.getByText(/^Onboarded databases/).locator('xpath=following::table[1]')
      await rowNamed(onboarded, 'orders-db').getByText('Ready').waitFor()
    })

    await h.step('onboard the Aurora cluster with a reader', a, async () => {
      const arn = fake.putSecret(fake.secretNameFor(space, 'billing-cluster', 'aurora-postgresql'), { host: fake.instances.get('db-billing-cluster').endpoint })
      const dlg = await wizardPaste(page, 'billing-cluster', arn)
      await wizardTest(dlg)
      await dlg.getByText('✓ Secret validated').waitFor()
      await dlg.getByRole('button', { name: /^Onboard$/ }).click()
      await dlg.waitFor({ state: 'detached' })
      await h.eventually(async () => isVisible(page.getByText('Onboarded databases (2)')), { what: 'billing-cluster as an onboarded database' })
      const onboarded = page.getByText(/^Onboarded databases/).locator('xpath=following::table[1]')
      await rowNamed(onboarded, 'billing-cluster').getByText('1 writer, 1 reader').waitFor()
      await rowNamed(onboarded, 'billing-cluster').getByText('Ready').waitFor()
    }, { checkpoint: 'onboarded' })

    // From here the admin walks on with the stages' own Next buttons, as a person does
    // after onboarding: Databases → Agent capabilities → Operators → Topology.
    await h.step('operators: nobody until someone is added', a, async () => {
      await nextTo(page, 'Agent capabilities')
      await nextTo(page, 'Operators')
      await page.getByText('No users assigned to this space.').waitFor()
      const table = page.getByText('No users assigned to this space.').locator('xpath=ancestor::table[1]')
      h.expect(!(await table.innerText()).includes(a.email), 'the creator is listed as an operator', 5)
    }, { checkpoint: 'operators-empty' })

    await h.step('Next to Topology: the map draws both databases in view, no refresh', a, async () => {
      const mark = fake.requests.length
      await nextTo(page, 'Topology')
      await h.eventually(async () => isVisible(page.locator('div', { hasText: /^2Databases$/ })), { what: 'the Topology tiles counting 2 databases' })
      h.expect(!(await isVisible(page.getByText('No topology data yet.'))), 'Topology says there is no data', 15)
      // The map itself, not just its tiles: each database box is drawn inside the visible
      // map area (the canvas clips what is outside it), without Refresh metadata or a reload.
      for (const db of ['orders-db', 'billing-cluster']) {
        await h.eventually(async () => (await boxInMap(page, db)).inside,
          { what: `the ${db} box inside the visible topology map`, timeout: 5000 })
      }
      h.expect(!fake.since(mark).some(r => r.path.endsWith('/topology/refresh')), 'Topology needed a metadata refresh', 15)
    }, { checkpoint: 'topology' })

    // The map must also draw when its canvas gets its size only after the map mounted (a
    // slow layout, a panel opening): the cause of the blank map until a reload. Hold the
    // canvas at 0 px while Topology is opened again with Next, then let it lay out.
    await h.step('the map fits itself when its canvas is laid out late', a, async () => {
      await openStage(page, 'Operators')
      await page.getByText('No users assigned to this space.').waitFor()
      const hold = await page.addStyleTag({ content: 'div:has(> svg[aria-label^="Network map"]) { flex: 0 0 0px !important; width: 0px !important; }' })
      await nextTo(page, 'Topology')
      const map = page.getByRole('img', { name: /^Network map of the agent/ })
      await map.waitFor({ state: 'attached' })
      await page.waitForTimeout(300)   // the map's mount effects run against the 0 px canvas
      h.expect(((await map.boundingBox())?.width || 0) < 1, 'the canvas was not held at 0 px (the step tests nothing)')
      await hold.evaluate(n => n.remove())
      for (const db of ['orders-db', 'billing-cluster']) {
        await h.eventually(async () => (await boxInMap(page, db)).inside,
          { what: `the ${db} box inside the topology map once its canvas is laid out`, timeout: 5000 })
      }
      const viewport = page.locator('svg[aria-label^="Network map"] [data-viewport]')
      const own = await viewport.getAttribute('transform')
      await page.getByRole('button', { name: 'Fit to view' }).click()
      await page.waitForTimeout(100)
      const fitted = await viewport.getAttribute('transform')
      h.expect(fitted === own, `the map was not fitted on its own (${own}; Fit to view gives ${fitted})`, 15)
    })

    await h.step('add an operator on purpose', a, async () => {
      await openStage(page, 'Operators')
      h.intendOperator(space.space_id, 'op-1')
      await page.getByRole('button', { name: /^Add user$/ }).click()
      await page.getByPlaceholder('Search by name or email...').fill('olga')
      await page.getByRole('button', { name: 'Grant access', exact: true }).click()
      await page.locator('tbody tr', { hasText: 'olga@example.com' }).waitFor()
      h.expect(fake.spaces.get(space.space_id).operators.size === 1, 'the backend does not hold exactly the one operator added')
    }, { checkpoint: 'operators-added' })

    await h.step('audit shows the actions', a, async () => {
      await page.getByRole('button', { name: /Audit log/ }).click()
      const log = page.getByRole('region', { name: 'Audit log' }).locator('tbody')
      for (const label of ['Created space', 'Validated account access', 'Ran discovery', 'Onboarded database', 'Added operator']) {
        await log.getByText(label, { exact: true }).first().waitFor()
      }
      h.expect((await log.getByText('Onboarded database', { exact: true }).count()) === 2, 'the audit log does not show both onboards')
    }, { checkpoint: 'audit' })
  },
}
