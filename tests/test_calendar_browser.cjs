// Start tests/browser_calendar_fixture.py; use an isolated localhost URL only.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const path = require('node:path');
const screenshots = process.env.SBWAGES_SCREENSHOT_DIR || require('node:os').tmpdir();
(async () => {
  const base = process.env.SBWAGES_TEST_URL;
  if (!base || !/^http:\/\/(127\.0\.0\.1|localhost):/.test(base)) throw Error('Local test URL required');
  const browser = await chromium.launch({headless:true, executablePath:process.env.CHROMIUM_PATH || undefined,args:['--no-sandbox']});
  try {
    const page = await browser.newPage({viewport:{width:1600,height:1000}});
    const errors=[];page.on('pageerror',e=>errors.push(e.message));
    page.on('console',m=>{if(m.type()==='error')errors.push(m.text());});
    await page.goto(base+'/admin/login');
    await page.locator('[name=username]').fill('admin');await page.locator('[name=password]').fill('test-admin-password');
    await page.getByRole('button',{name:'Sign in',exact:true}).click();
    await page.goto(base+'/binflow/collection-calendar?state=QLD&date=2026-11-02');
    const root=page.locator('[data-calendar-editor]'),panel=page.locator('#calendar-expanded-day');
    const cell=date=>root.locator('td[data-date="'+date+'"]');
    await cell('2026-10-26').getByRole('button',{name:'Expand QLD Monday 26 Oct'}).waitFor();
    await cell('2026-10-26').locator('[data-drag-date]').dragTo(cell('2026-10-28'),{targetPosition:{x:30,y:20}});
    await page.getByText('1 unsaved move. Review the dates, then Save changes.',{exact:true}).waitFor();
    assert.match(await cell('2026-10-26').innerText(),/Baling/);
    assert.match(await cell('2026-10-28').innerText(),/Alpha QLD/);
    await root.getByRole('button',{name:'Undo move',exact:true}).click();
    assert.match(await cell('2026-10-26').innerText(),/Alpha QLD/);
    await cell('2026-10-26').getByRole('button',{name:'Expand QLD Monday 26 Oct'}).click();
    await panel.waitFor({state:'visible'});
    assert.equal(await panel.locator('.bf-expanded-stop').count(),2);
    // Reorder using the actual draggable handle and drop target.
    await panel.locator('.bf-stop-grip').nth(1).dragTo(panel.locator('.bf-expanded-stop').first());
    await page.getByText('1 unsaved move. Review the dates, then Save changes.',{exact:true}).waitFor();
    assert.match(await panel.locator('.bf-expanded-stop').first().innerText(),/Beta QLD/);
    // Drag out of the modeless expanded view onto the month calendar, across weeks.
    await panel.locator('.bf-stop-grip').nth(1).dragTo(cell('2026-11-03'),{targetPosition:{x:30,y:20}});
    await page.getByText('2 unsaved moves. Review the dates, then Save changes.',{exact:true}).waitFor();
    assert.match(await cell('2026-11-03').innerText(),/Alpha QLD/);
    await page.screenshot({path:path.join(screenshots,'calendar-desktop.png'),fullPage:false});
    await panel.getByRole('button',{name:'Save changes',exact:true}).click();
    await page.waitForLoadState('networkidle');
    await root.locator('[data-calendar-message]').filter({hasText:'Calendar changes saved.'}).waitFor();
    assert.match(await cell('2026-11-03').innerText(),/Alpha QLD/);
    await panel.getByRole('button',{name:'Close expanded day'}).click();
    await cell('2026-11-03').getByRole('button',{name:'Expand QLD Tuesday 3 Nov'}).click();
    assert.match(await panel.innerText(),/Alpha QLD/);
    // A failed save keeps edits on screen and allows a safe retry.
    await panel.locator('[data-whole-day-target]').fill('2026-11-04');
    await panel.getByRole('button',{name:'Move day',exact:true}).click();
    await page.getByText('1 unsaved move. Review the dates, then Save changes.',{exact:true}).waitFor();
    await page.route('**/binflow/schedule/calendar-save',route=>route.abort());
    await panel.getByRole('button',{name:'Save changes',exact:true}).click();
    await root.locator('[data-calendar-message].is-error').waitFor();
    assert.equal(await panel.getByRole('button',{name:'Save changes',exact:true}).isEnabled(),true);
    await page.unroute('**/binflow/schedule/calendar-save');
    await panel.getByRole('button',{name:'Save changes',exact:true}).click();
    await page.waitForLoadState('networkidle');
    await root.locator('[data-calendar-message]').filter({hasText:'Calendar changes saved.'}).waitFor();
    // Phone controls can move the same stop without HTML drag-and-drop support.
    await page.setViewportSize({width:390,height:844});
    await panel.locator('.bf-expanded-stop').getByRole('button',{name:'Move…',exact:true}).click();
    await page.locator('[data-stop-target]').fill('2026-11-05');
    await page.locator('#calendar-stop-move').getByRole('button',{name:'Move stop',exact:true}).click();
    await page.locator('#calendar-stop-move').waitFor({state:'hidden'});
    await panel.getByRole('button',{name:'Save changes',exact:true}).click();
    await page.waitForLoadState('networkidle');
    await root.locator('[data-calendar-message]').filter({hasText:'Calendar changes saved.'}).waitFor();
    await panel.getByRole('button',{name:'Next day',exact:true}).click();
    await panel.getByText('Alpha QLD',{exact:true}).waitFor();
    const bounds=await panel.boundingBox();assert(bounds.x>=0&&bounds.x+bounds.width<=390);
    await panel.getByRole('button',{name:'Enlarge day view'}).click();
    await page.screenshot({path:path.join(screenshots,'calendar-mobile.png'),fullPage:false});
    assert.deepEqual(errors.filter(e=>!e.includes('ERR_FAILED')),[]);
    console.log('PASS: desktop day drag, undo, expansion, reorder, cross-week stop drag, save/reload, network retry and mobile date controls');
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
