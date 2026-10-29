const path = require('path');
const fs = require('fs');
const { chromium } = require(path.resolve(__dirname, '../frontend/node_modules/playwright'));

async function capture() {
  const port = process.env.PORT || 19876;
  const outDir = path.resolve(__dirname, '../docs/images');
  if (!fs.existsSync(outDir)) {
    fs.mkdirSync(outDir, { recursive: true });
  }

  const browser = await chromium.launch({
    headless: true,
    args: ['--no-sandbox', '--disable-setuid-sandbox', '--font-render-hinting=none'],
  });

  const context = await browser.newContext({
    viewport: { width: 1440, height: 960 },
    deviceScaleFactor: 2, // HiDPI / Retina crispness
  });

  const page = await context.newPage();

  console.log(`Navigating to http://127.0.0.1:${port}...`);
  await page.goto(`http://127.0.0.1:${port}/`, { waitUntil: 'networkidle' });

  // Wait for stats and sessions to load
  await page.waitForSelector('.stats');
  await page.waitForSelector('.session-table');

  // Brief pause for animations/charts
  await page.waitForTimeout(800);

  const overviewPath = path.join(outDir, 'dashboard-overview.png');
  console.log(`Saving overview to ${overviewPath}...`);
  await page.screenshot({ path: overviewPath, fullPage: false });

  // Open the first session (Antigravity)
  console.log('Opening session drawer...');
  const firstSession = page.locator('.session-button').first();
  await firstSession.click();
  await page.waitForSelector('.drawer');
  await page.waitForTimeout(600);

  // Inspect prompt activity
  const inspectBtn = page.getByRole('button', { name: 'Inspect prompt activity' }).first();
  if (await inspectBtn.isVisible()) {
    await inspectBtn.click();
    await page.waitForTimeout(600);
  }

  // Open the first tool call card summary if present
  const toolSummary = page.locator('.tool-card summary').first();
  if (await toolSummary.isVisible()) {
    await toolSummary.click();
  }

  await page.waitForTimeout(600);

  const sessionPath = path.join(outDir, 'session-inspector.png');
  console.log(`Saving session inspector to ${sessionPath}...`);
  await page.screenshot({ path: sessionPath, fullPage: false });

  await browser.close();
  console.log('Screenshots captured successfully!');
}

capture().catch((err) => {
  console.error('Capture failed:', err);
  process.exit(1);
});
