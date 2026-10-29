// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
//
// S7 add-account wizard (rules 17, 18): the space admin opens "Add secondary cloud
// source" and types an account id; the wizard asks the server whether the admin's access
// role is already usable there and shows one of four states:
//   ready     the role is there with the current policy: nothing to run;
//   outdated  the role is there, its policy is older: only the update command;
//   missing   no usable role: the create command and the manual walkthrough;
//   denied    the role is known (another space validated it) but its trust refuses:
//             the reason and the trust-policy command.
// The role name is the shared per-admin name, read-only until "Use a different name";
// a name outside the deployment's prefix is refused with the fix. Registering a ready
// account adds it to the space, pending validation, with the owner admin as External ID.
import { standardUsers } from '../lib/harness.mjs'
import { openStage, isVisible } from '../lib/ui.mjs'
import { MEMBER_POLICY_VERSION } from '../fake_api.mjs'

const READY = '111111111111'
const OUTDATED = '222222222222'
const MISSING = '333333333333'
const DENIED = '444444444444'
const SHEET = '[role="dialog"][aria-label="Add secondary cloud source"]'

export default {
  id: 'S7',
  name: 'add-account wizard',
  async run(h) {
    const fake = h.fake
    standardUsers(fake)
    const owner = fake.userByKey('admin-a').sub
    const roleName = fake.agentRoleName(owner)
    const payments = fake.seedSpace('admin-a', { name: 'Payments' })
    // Another of admin A's spaces already uses (and validated) the role in two accounts.
    const orders = fake.seedSpace('admin-a', { name: 'Orders' })
    fake.seedAccount(orders, READY)
    fake.seedAccount(orders, DENIED)
    fake.putMemberRole(READY, roleName, { trusts: [owner], version: MEMBER_POLICY_VERSION })
    fake.putMemberRole(OUTDATED, roleName, { trusts: [owner], version: null })   // created before the version tag
    fake.putMemberRole(DENIED, roleName, { trusts: [] })                          // its trust was edited since

    const a = await h.open('admin', 'admin-a')
    const page = a.page
    const sheet = page.locator(SHEET)
    const accountId = page.locator('#add-account-id')
    const roleInput = page.locator('#add-account-role')
    const checks = () => fake.requests.filter(r => r.method === 'POST' && r.path === `/api/spaces/${payments}/accounts/check`)
    const commands = async () => (await sheet.locator('pre:visible').allInnerTexts()).join('\n')
    const ctx = { space: payments, region: SHEET }
    // Types an account id and waits for the check: the spinner line, then the answer.
    const typeAccount = async (id) => {
      const n = checks().length
      await accountId.fill(id)
      await sheet.getByText(`Checking account ${id}…`).waitFor({ timeout: 3000 })
      await sheet.getByText(`Checking account ${id}…`).waitFor({ state: 'detached', timeout: 5000 })
      h.expect(checks().length === n + 1, `typing ${id} sent ${checks().length - n} account checks, not one`)
      return checks().at(-1).body
    }

    await h.step('open the wizard: the shared role name, read-only', a, async () => {
      await page.getByRole('button', { name: 'Payments', exact: true }).first().click()
      await openStage(page, 'Accounts & regions')
      await page.getByRole('button', { name: 'Add', exact: true }).click()
      await sheet.waitFor()
      h.expect(await roleInput.inputValue() === roleName, `the role name is ${await roleInput.inputValue()}, not ${roleName}`)
      h.expect((await roleInput.getAttribute('readonly')) !== null, 'the role name is editable before "Use a different name"')
      h.expect(!checks().length, 'the wizard checked an account before one was typed')
    }, { ...ctx, checkpoint: 'wizard-open' })

    await h.step('ready: the role is there, nothing to run', a, async () => {
      const body = await typeAccount(READY)
      h.expect(body.account_id === READY && body.role_name === roleName, `the check asked about ${JSON.stringify(body)}`)
      await sheet.getByText('Your access role is already in this account (used by your space Orders). Nothing to run.').waitFor()
      h.expect(!(await commands()).includes('create-role'), 'a ready account still shows the create-role command')
      const fold = sheet.locator('details', { hasText: "Update the role's permissions" })
      h.expect(await fold.isVisible() && (await fold.getAttribute('open')) === null, 'the update command is not folded away')
      h.expect(await sheet.getByRole('button', { name: 'Next', exact: true }).isEnabled(), 'Next is disabled for a ready account')
    }, { ...ctx, checkpoint: 'ready' })

    await h.step('outdated: only the update command', a, async () => {
      await typeAccount(OUTDATED)
      await sheet.getByText('Your role is there, but its permissions are older than this version.', { exact: false }).waitFor()
      const cmd = await commands()
      h.expect(cmd.includes('put-role-policy') && cmd.includes(`DBOpsPolicyVersion,Value=${MEMBER_POLICY_VERSION}`),
        'the update command does not re-apply the policy and its version tag')
      h.expect(!cmd.includes('create-role'), 'an outdated account shows the create-role command')
    }, { ...ctx, checkpoint: 'outdated' })

    await h.step('missing: the create command and the manual walkthrough', a, async () => {
      await typeAccount(MISSING)
      await sheet.getByText(`No role this deployment can use was found in account ${MISSING}. Create it with the command below.`).waitFor()
      const cmd = await commands()
      h.expect(cmd.includes('aws iam create-role') && cmd.includes(`--role-name ${roleName}`), 'the create command is not shown for the shared role')
      h.expect(await isVisible(sheet.getByText('Do it manually in the IAM console instead')), 'the manual walkthrough is not offered')
    }, { ...ctx, checkpoint: 'missing' })

    await h.step('denied: the reason and the trust-policy command', a, async () => {
      await typeAccount(DENIED)
      const alert = sheet.getByRole('alert')
      await alert.getByText(`${roleName} is in account ${DENIED} (your space Orders validated it), but its trust policy no longer accepts this deployment.`, { exact: false }).waitFor()
      h.expect((await alert.innerText()).includes(`sts:ExternalId ${owner}`), 'the reason does not say which External ID the trust must accept', 18)
      await sheet.getByText(`Re-apply the trust policy in account ${DENIED}`).waitFor()
      const cmd = await commands()
      h.expect(cmd.includes('update-assume-role-policy') && !cmd.includes('create-role'), 'the denied state does not show (only) the trust-policy command')
    }, { ...ctx, checkpoint: 'denied' })

    await h.step('a different name is checked again; one outside the prefix is refused with the fix', a, async () => {
      await accountId.fill(MISSING)
      await sheet.getByText(/Create it with the command below\./).waitFor()
      await sheet.getByRole('button', { name: 'Use a different name' }).click()
      h.expect((await roleInput.getAttribute('readonly')) === null, 'the role name stays read-only after "Use a different name"')
      const n = checks().length
      await roleInput.fill('acme-reader')
      await sheet.getByText('This deployment may not assume acme-reader: its role can assume only roles named dbops-*. Use a name that starts with dbops-.').waitFor()
      h.expect(checks().slice(n).at(-1)?.body?.role_name === 'acme-reader', 'the check did not run again with the typed name')
    }, { ...ctx, checkpoint: 'custom-name' })

    await h.step('register a ready account: added, pending validation, owner as External ID', a, async () => {
      await sheet.getByRole('button', { name: 'Cancel', exact: true }).click()
      await sheet.waitFor({ state: 'detached' })
      await page.getByRole('button', { name: 'Add', exact: true }).click()
      h.expect(await roleInput.inputValue() === roleName, 'reopening the wizard did not reset the role name')
      await typeAccount(READY)
      await sheet.getByText(/Nothing to run\./).waitFor()
      await sheet.getByRole('button', { name: 'Next', exact: true }).click()
      const row = (label) => sheet.locator('div', { has: page.getByText(label, { exact: true }) }).last()
      h.expect((await row('External ID').innerText()).includes(owner), 'step 2 does not show the owner admin as the External ID')
      h.expect((await row('Role').innerText()).includes(`arn:aws:iam::${READY}:role/${roleName}`), 'step 2 does not show the role ARN')
      const mark = fake.requests.length
      await sheet.getByRole('button', { name: 'Add account', exact: true }).click()
      await sheet.waitFor({ state: 'detached' })
      const puts = fake.since(mark).filter(r => r.method === 'PUT' && r.path === `/api/spaces/${payments}`)
      h.expect(puts.length === 1, `registering sent ${puts.length} updates`)
      const s = fake.spaces.get(payments)
      h.expect(s.secondary_account_ids.includes(READY) && s.secondary_role_names[READY] === roleName, 'the space does not hold the account and its role')
      const listed = page.locator('tbody tr', { hasText: READY })
      await listed.getByText('Pending validation').waitFor()
      h.expect((await listed.innerText()).includes(roleName), 'the account row does not show its role')
    }, { space: payments, checkpoint: 'registered' })
  },
}
