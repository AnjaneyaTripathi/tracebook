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
  await page.getByRole('button', { name: 'Inspect prompt activity' }).first().click();
  const activity = page.getByRole('region', { name: 'Prompt activity' });
  await expect(activity.getByRole('heading', { name: '2 model calls · 2 tools' })).toBeVisible();
  await expect(activity.locator('.prompt-metrics').first()).toContainText('Cache read');
  await expect(activity.locator('.prompt-metrics').first()).toContainText('$0.0600');
  await activity.locator('.call-card summary').first().click();
  await expect(activity.locator('.call-card').first()).toContainText('Input uncached');
  await activity.locator('.tool-card summary').first().click();
  await expect(activity.locator('.tool-card').first()).toContainText('pytest');
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
  await expect(page.getByRole('dialog', { name: 'Generated report' })).toContainText('Tokens by model');
  await expect(page.getByRole('dialog', { name: 'Generated report' })).toContainText('Cost by model');
  await expect(page.getByRole('button', { name: 'Download Markdown' })).toBeVisible();
  expect(errors).toEqual([]);
});

test('prompt details distinguish missing usage from zero and recover from a failed request', async ({ page }) => {
  await page.goto('/');
  await page.locator('.session-button').first().click();
  await page.route('**/api/turns/*', route => route.fulfill({ status: 503, body: '{}' }), { times: 1 });
  await page.getByRole('button', { name: 'Inspect prompt activity' }).first().click();
  await expect(page.getByRole('alert')).toContainText('could not be loaded');
  await page.route('**/api/turns/*', async route => {
    const response = await route.fetch();
    const detail = await response.json();
    detail.calls = [{ ...detail.calls[0], input_uncached: 0, input_cache_read: null, input_cache_write: null, output: 0, reasoning: null, total_reported: null, cost_reported: null, cost_estimated: null }];
    await route.fulfill({ response, json: detail });
  });
  await page.getByRole('button', { name: 'Retry', exact: true }).click();
  const activity = page.getByRole('region', { name: 'Prompt activity' });
  const metrics = activity.locator('.prompt-metrics').first();
  await expect(metrics.locator('div').filter({ hasText: 'Input uncached' }).locator('dd')).toHaveText('0');
  await expect(metrics.locator('div').filter({ hasText: 'Cache read' }).locator('dd')).toHaveText('—');
  await expect(metrics.locator('div').filter({ hasText: 'Cost reported' }).locator('dd')).toHaveText('—');
  await expect(activity).toContainText('1 call has no known price');
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
    await page.locator('.session-button').first().click();
    await page.getByRole('button', { name: 'Inspect prompt activity' }).first().click();
    await expect(page.getByRole('region', { name: 'Prompt activity' }).getByRole('heading', { name: 'Model calls', exact: true })).toBeVisible();
    await expect.poll(() => page.locator('.drawer').evaluate(element => element.scrollWidth > element.clientWidth)).toBe(false);
    await page.screenshot({ path: `test-results/prompt-${width === 390 ? 'mobile' : 'desktop'}.png`, fullPage: false });
  });
}

test('session table sorts, filters and hides sessions without usage', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('button', { name: 'View all sessions' }).click();
  const rows = page.locator('.session-table tbody tr');
  await expect(page.getByText('1–25 of 42')).toBeVisible();
  const tokens = page.getByRole('columnheader', { name: /Tokens/ });
  const column = async () =>
    (await rows.locator('td.numeric:nth-child(4)').allInnerTexts()).map((v) =>
      v.endsWith('K') ? parseFloat(v) * 1000 : parseFloat(v)
    );
  const sorted = (values: number[], direction: 1 | -1) =>
    values.every((v, i) => i === 0 || (values[i - 1] - v) * direction >= 0);
  await tokens.getByRole('button').click();
  await expect(tokens).toHaveAttribute('aria-sort', 'descending');
  await expect.poll(async () => sorted(await column(), 1)).toBe(true);
  await tokens.getByRole('button').click();
  await expect(tokens).toHaveAttribute('aria-sort', 'ascending');
  await expect.poll(async () => sorted(await column(), -1)).toBe(true);
  expect((await column()).length).toBe(25);
  await page.getByLabel('Project').selectOption({ label: 'design-system (14)' });
  await expect(page.getByText('1–14 of 14')).toBeVisible();
  await expect(page.getByLabel('Started')).toHaveValue('30');
  await page.getByLabel('Started').selectOption('');
  await page.getByRole('button', { name: 'Reset' }).click();
  await expect(page.getByLabel('Started')).toHaveValue('30');
  await expect(page.getByText('(1 hidden)')).toBeVisible();
  await page.getByLabel('Show sessions with no usage').check();
  await expect(page.getByText('1–25 of 43')).toBeVisible();
  await page.getByRole('combobox', { name: 'Model' }).selectOption('example-model');
  await expect(page.getByText('1–25 of 42')).toBeVisible();
});
