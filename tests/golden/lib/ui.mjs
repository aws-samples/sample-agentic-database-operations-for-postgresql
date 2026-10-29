// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
//
// Small helpers the scenarios share: finding things the way a person does (by visible
// name and role), and the admin console's common journeys.

const esc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

// The table row whose FIRST cell starts with `name` (so "orders-db" never matches the
// "orders-replica" row, whose sub-label reads "replica of orders-db").
export function rowNamed(scope, name) {
  return scope.locator('tbody tr').filter({ has: scope.page().locator('td:first-child', { hasText: new RegExp(`^\\W*${esc(name)}`) }) }).first()
}

export const stage = (page, label) =>
  page.getByRole('navigation', { name: 'Space setup' }).getByRole('button', { name: new RegExp(esc(label)) })

export async function openSpace(page, name) {
  await page.getByRole('button', { name, exact: true }).first().click()
  await page.waitForLoadState('networkidle')
}

export async function openStage(page, label) {
  await stage(page, label).click()
  await page.waitForLoadState('networkidle')
}

// The stage footer's "Next: <label>" button (the in-app way forward, no reload).
export async function nextTo(page, label) {
  await page.getByRole('button', { name: new RegExp(`^Next: ${esc(label)}`) }).click()
  await page.waitForLoadState('networkidle')
}
// Where the topology map drew database `name`: its box must lie inside the map's
// visible area (the canvas clips the rest), and both must have a real size.
export async function boxInMap(page, name) {
  const map = page.getByRole('img', { name: /^Network map of the agent/ })
  const c = await map.boundingBox().catch(() => null)
  const db = await map.getByRole('button', { name: new RegExp(`^${esc(name)}, health`) }).first().boundingBox({ timeout: 500 }).catch(() => null)
  const inside = !!(c && db && c.width > 100 && c.height > 100 && db.width > 4 && db.height > 4 &&
    db.x >= c.x && db.y >= c.y && db.x + db.width <= c.x + c.width && db.y + db.height <= c.y + c.height)
  return { inside, map: c, box: db }
}
export async function isVisible(locator) {
  try { return await locator.first().isVisible() } catch { return false }
}

// Opens the onboarding wizard for `dbName`, moves to Step 2 and pastes `arn`.
// Returns the wizard dialog.
export async function wizardPaste(page, dbName, arn) {
  const found = page.getByText(/Found, not yet onboarded/).locator('xpath=following::table[1]')
  await rowNamed(found, dbName).getByRole('button', { name: /Onboard|Add to this space/ }).click()
  const dlg = page.getByRole('dialog', { name: /Onboard database|Add database to this space/ })
  await dlg.waitFor()
  if (await isVisible(dlg.getByRole('button', { name: 'Next', exact: true }))) await dlg.getByRole('button', { name: 'Next', exact: true }).click()
  await dlg.locator('input[placeholder^="arn:aws:secretsmanager"]').fill(arn)
  return dlg
}

export async function wizardTest(dlg) {
  await dlg.getByRole('button', { name: /^Test secret$/ }).click()
  await dlg.getByRole('button', { name: /^Test secret$/ }).waitFor({ timeout: 5000 })
  await dlg.page().waitForLoadState('networkidle')
}
