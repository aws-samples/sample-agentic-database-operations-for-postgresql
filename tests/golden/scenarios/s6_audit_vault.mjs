// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
//
// S6 audit vault (rule 24): turning the vault off needs the typed confirmation (a wrong
// phrase cannot submit), is recorded before it takes effect, and the Security tab then
// shows the reduced state.
import { standardUsers } from '../lib/harness.mjs'
import { VAULT_OFF_PHRASE } from '../fake_api.mjs'

export default {
  id: 'S6',
  name: 'audit vault',
  async run(h) {
    const fake = h.fake
    standardUsers(fake)
    const s = await h.open('admin', 'super')
    const page = s.page
    const rail = page.locator('nav, aside').first()

    await h.step('Settings → Audit shows the vault on', s, async () => {
      await page.getByRole('button', { name: 'Settings', exact: true }).click()
      await rail.getByRole('button', { name: 'Audit', exact: true }).click()
      await page.getByRole('switch').waitFor()
      h.expect((await page.getByRole('switch').getAttribute('aria-checked')) === 'true', 'the vault is not shown on')
    }, { checkpoint: 'vault-on' })

    const dialog = page.getByRole('dialog', { name: 'Turn off the audit vault?' })
    const submit = dialog.getByRole('button', { name: 'Turn off the vault' })
    await h.step('switching it off asks for the typed phrase; a wrong one cannot submit', s, async () => {
      await page.getByRole('switch').click()
      await dialog.waitFor()
      h.expect(await submit.isDisabled(), 'the vault can be turned off without typing anything', 24)
      await dialog.locator('input').fill('turn off')
      h.expect(await submit.isDisabled(), 'a wrong phrase can submit', 24)
      h.expect(fake.auditSettings.vault_enabled, 'the vault went off before the confirmation')
    }, { checkpoint: 'confirm-dialog', region: '[role="dialog"]' })

    await h.step('the right phrase turns it off, recorded first', s, async () => {
      await dialog.locator('input').fill(VAULT_OFF_PHRASE)
      await submit.click()
      await dialog.waitFor({ state: 'detached' })
      await page.getByText('Audit log: index only, not tamper-proof.').first().waitFor()
      h.expect((await page.getByRole('switch').getAttribute('aria-checked')) === 'false', 'the switch still reads on')
      const rec = fake.audit.find(r => r.action === 'audit_vault_set')
      h.expect(rec && rec.vault === 'stored', 'the switch-off was not recorded in the vault before it took effect', 24)
      await page.getByRole('region', { name: 'Audit log' }).locator('tbody tr', { hasText: 'Switched audit vault' }).waitFor()
    }, { checkpoint: 'vault-off' })

    await h.step('the Security tab shows the reduced state', s, async () => {
      await rail.getByRole('button', { name: 'Security', exact: true }).click()
      const card = page.locator('div.rounded-xl', { has: page.getByRole('heading', { name: 'Admin audit vault' }) })
      await card.waitFor()
      await card.getByText('Off', { exact: true }).waitFor()
      await card.getByText(/index only, not tamper-proof/).waitFor()
    }, { checkpoint: 'security-reduced' })
  },
}
