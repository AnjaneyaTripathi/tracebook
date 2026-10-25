import { test, expect } from '@playwright/test';

test('explore sessions, evidence, findings, coverage and a report', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('/');
  await expect(page.getByText('Daemon connected')).toBeVisible();
  await expect(page.getByRole('heading', { name: 'A clear view of the work.' })).toBeVisible();
  await expect(page.locator('.stats')).toContainText('126');
  await page.getByRole('button', { name: 'View all sessions' }).click();
  await page.getByRole('button', { name: 'OpenAI Codex', exact: true }).click();
  await expect(page.locator('.session-table tbody tr')).toHaveCount(14);
  await page.getByRole('searchbox', { name: 'Search prompts' }).fill('checkout');
  await expect(page.locator('.session-table tbody tr')).not.toHaveCount(14);
  await page.locator('.session-button').first().click();
  await expect(page.getByRole('dialog', { name: 'Session details' })).toBeVisible();
  await expect(page.locator('.turn-card')).toHaveCount(3);
  await page.getByRole('button', { name: 'Tools', exact: true }).click();
  await page.locator('.tool-card').first().click();
  await expect(page.locator('.tool-card').first()).toContainText('pytest');
  await page.getByRole('button', { name: 'Evidence', exact: true }).click();
  await page.locator('.evidence').first().click();
  await expect(page.locator('.evidence').first()).toContainText('provenance');
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await page.getByRole('button', { name: 'Coverage', exact: true }).click();
  await expect(page.locator('.coverage-table')).toContainText('unverified');
  await page.getByRole('button', { name: 'Reports', exact: true }).click();
  await page.getByRole('button', { name: 'Generate report' }).click();
  await expect(page.getByRole('dialog', { name: 'Generated report' })).toContainText('not billed spend');
  await expect(page.getByRole('button', { name: 'Download Markdown' })).toBeVisible();
  expect(errors).toEqual([]);
});

for (const width of [1440, 390]) {
  test(`overview and navigation at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 1060 });
    await page.goto('/');
    await expect(page.locator('.stats')).toContainText('126');
    await expect(page.getByRole('heading', { name: 'A clear view of the work.' })).toBeVisible();
    await expect(page.getByRole('navigation')).toBeVisible();
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth > innerWidth)).toBe(false);
    await page.screenshot({ path: `test-results/overview-${width === 390 ? 'mobile' : 'desktop'}.png`, fullPage: false });
    await page.getByRole('button', { name: 'Sessions', exact: true }).click();
    await expect(page.getByRole('searchbox')).toBeVisible();
  });
}
