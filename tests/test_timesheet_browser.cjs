// Start: python tests/browser_timesheet_fixture.py
// Run: SBWAGES_TEST_URL=http://127.0.0.1:5087 node tests/test_timesheet_browser.cjs
// Requires Playwright; CHROMIUM_PATH optionally selects an installed browser.
// SBWAGES_TEST_URL must never point at production.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
(async () => {
  const base = process.env.SBWAGES_TEST_URL;
  if (!base || !/^http:\/\/(127\.0\.0\.1|localhost):/.test(base)) throw Error('Local test URL required');
  const browser = await chromium.launch({headless: true, executablePath: process.env.CHROMIUM_PATH || undefined, args: ['--no-sandbox']});
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('dialog', dialog => dialog.accept());
    await page.goto(base + '/employee/login');
    await page.locator('[name=username]').fill('alex');
    await page.locator('[name=password]').fill('test-staff-password');
    await page.getByRole('button', {name:'Sign in',exact:true}).click();
    await page.goto(base + '/employee/timesheet?week=2026-09-20');
    const row = n => page.locator('.day-row').nth(n);
    const fill = async(n, note) => {
      await row(n).locator('[name=start_time]').fill('08:00');
      await row(n).locator('[name=finish_time]').fill('16:00');
      await row(n).locator('[name=activity]').fill(note);
    };
    const waitStatus = (n,text) => row(n).locator('[role=status]').filter({hasText:text}).waitFor();
    await fill(0, 'Monday work');
    await fill(1, 'Tuesday unsaved work');
    let navigations = 0;
    page.on('framenavigated', () => navigations++);
    await row(0).getByRole('button',{name:'Save day',exact:true}).click();
    await waitStatus(0, 'Day saved.');
    assert.equal(await row(1).locator('[name=activity]').inputValue(), 'Tuesday unsaved work');
    await row(0).getByRole('button',{name:'Commit day',exact:true}).click();
    await waitStatus(0, 'Day committed and locked.');
    assert.equal(await row(0).locator('[name=start_time]').count(), 0);
    assert.equal(await row(1).locator('[name=activity]').inputValue(), 'Tuesday unsaved work');
    assert.equal(navigations, 0);
    await row(1).locator('[name=activity]').fill('');
    await row(1).getByRole('button',{name:'Save day',exact:true}).click();
    await waitStatus(1, 'Add activity notes');
    assert.equal(await row(1).locator('[name=start_time]').inputValue(), '08:00');
    await row(1).locator('[name=activity]').fill('Tuesday saved work');
    await row(1).getByRole('button',{name:'Save day',exact:true}).click();
    await waitStatus(1, 'Day saved.');
    await page.reload();
    assert.match(await row(0).innerText(), /Committed · Locked/);
    assert.equal(await row(1).locator('[name=activity]').inputValue(), 'Tuesday saved work');
    await fill(2, 'Keep after network failure');
    await page.route('**/employee/timesheet?week=*', route => route.request().method() === 'POST' ? route.abort() : route.continue());
    await row(2).getByRole('button',{name:'Save day',exact:true}).click();
    await waitStatus(2, 'Connection failed');
    assert.equal(await row(2).locator('[name=activity]').inputValue(), 'Keep after network failure');
    assert.equal(await row(2).getByRole('button',{name:'Save day',exact:true}).isEnabled(), true);
    await page.unroute('**/employee/timesheet?week=*');
    await row(2).getByRole('button',{name:'Save day',exact:true}).click();
    await waitStatus(2, 'Day saved.');
    await page.reload();
    assert.equal(await row(2).locator('[name=activity]').inputValue(), 'Keep after network failure');
    assert.deepEqual(errors, []);
    console.log('PASS: multiple-day edits, save, commit, errors, network failure/retry, reload persistence');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
