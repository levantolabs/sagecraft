// Offline renderer qualification. Launches headless Chrome with fake snapshots only.
// Usage: node tests/test_spectator_hud.mjs
import {spawn} from 'node:child_process';
import fs from 'node:fs/promises';
import path from 'node:path';
import assert from 'node:assert/strict';
import {fileURLToPath, pathToFileURL} from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const assets = path.join(root, 'src/sage_wow/dashboard/spectator_assets');
const output = await fs.mkdtemp('/tmp/sage-spectator-renderer-');
const profile = path.join(output, 'chrome-profile');
await fs.mkdir(profile);
const chromePath = process.env.SAGE_HUD_CHROME || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const chrome = spawn(chromePath, ['--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check', '--remote-debugging-port=0', `--user-data-dir=${profile}`, 'about:blank'], {stdio: 'ignore'});
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
const result = {checks: [], errors: [], externalRequests: [], screenshots: [], output};
let socket;
try {
  let port;
  for (let i = 0; i < 100; i++) {
    try { port = (await fs.readFile(path.join(profile, 'DevToolsActivePort'), 'utf8')).split('\n')[0]; break; }
    catch { await wait(100); }
  }
  assert.ok(port, 'headless browser started');
  const pages = await (await fetch(`http://127.0.0.1:${port}/json`)).json();
  socket = new WebSocket(pages.find(page => page.type === 'page').webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.addEventListener('open', resolve, {once: true});
    socket.addEventListener('error', reject, {once: true});
  });
  let serial = 0;
  const pending = new Map();
  socket.addEventListener('message', event => {
    const message = JSON.parse(event.data);
    if (message.id) {
      const request = pending.get(message.id);
      pending.delete(message.id);
      message.error ? request.reject(Error(JSON.stringify(message.error))) : request.resolve(message.result);
    } else if (message.method === 'Runtime.exceptionThrown') result.errors.push(message.params.exceptionDetails);
    else if (message.method === 'Network.requestWillBeSent' && /^https?:/.test(message.params.request.url)) result.externalRequests.push(message.params.request.url);
  });
  const call = (method, params = {}) => new Promise((resolve, reject) => {
    const id = ++serial;
    pending.set(id, {resolve, reject});
    socket.send(JSON.stringify({id, method, params}));
  });
  const evaluate = async expression => {
    const reply = await call('Runtime.evaluate', {expression, returnByValue: true, awaitPromise: true});
    if (reply.exceptionDetails) throw Error(JSON.stringify(reply.exceptionDetails));
    return reply.result.value;
  };
  const check = async (name, expression) => {
    const passed = await evaluate(expression);
    result.checks.push({name, passed});
    assert.equal(passed, true, name);
  };
  const screenshot = async name => {
    const reply = await call('Page.captureScreenshot', {format: 'png', captureBeyondViewport: false});
    const filename = path.join(output, name);
    await fs.writeFile(filename, Buffer.from(reply.data, 'base64'));
    result.screenshots.push(filename);
  };
  const base = {session_id: 'fixture-session', source: 'fixture-only', source_revision: 1, state: 'PLAYING', phase: 'thinking', actor: 'sage', request_id: 'request-1', request_elapsed: 2.432, current_level: 3, goal_level: 5, choice_label: 'Target a wolf', decision_id: 'decision-0', decision_at: Date.now() / 1000 - 20};
  const push = async overrides => {
    const value = {...base, generated_at: Date.now() / 1000, ...overrides};
    await evaluate(`window.updateSageHUD(${JSON.stringify(value)})`);
  };
  await call('Page.enable');
  await call('Runtime.enable');
  await call('Network.enable');
  await call('Emulation.setDefaultBackgroundColorOverride', {color: {r: 0, g: 0, b: 0, a: 0}});
  await call('Emulation.setDeviceMetricsOverride', {width: 1496, height: 967, deviceScaleFactor: 1, mobile: false});
  await call('Page.navigate', {url: pathToFileURL(path.join(assets, 'index.html')).href});
  for (let i = 0; i < 100; i++) {
    if (await evaluate("document.readyState === 'complete' && typeof window.updateSageHUD === 'function'")) break;
    await wait(50);
  }
  await check('starts unavailable with no fabricated decision', "document.getElementById('hud').classList.contains('is-idle') && document.getElementById('timer-value').textContent === '—'");
  await check('document transparent and input transparent', "getComputedStyle(document.body).backgroundColor === 'rgba(0, 0, 0, 0)' && getComputedStyle(document.body).pointerEvents === 'none'");
  await check('alpha assets loaded locally', 'Array.from(document.images).every(image => image.complete && image.naturalWidth > 0)');
  await push({reaction: {id: 'historical', kind: 'kill', at: Date.now() / 1000, caption: 'Historical reaction'}});
  await check('hydration suppresses old celebrations', "document.getElementById('reaction').hidden && !document.getElementById('decision-card').classList.contains('punch')");
  await wait(300);
  await check('real pending request interpolates without inventing a new phase', "document.getElementById('hud').classList.contains('phase-thinking') && parseFloat(document.getElementById('timer-value').textContent) > 2.6");
  await check('landscape card remains compact at specified coordinates', "(() => {const r=document.getElementById('decision-card').getBoundingClientRect();return r.x===24 && r.y===48 && r.width===420 && r.height<=220;})()");
  await push({phase: 'executing', actor: 'harness', execution_label: 'Casting Smite', response_ms: 2432, server_ms: 1300, decision_id: 'decision-1', decision_at: Date.now() / 1000, reaction: {id: 'kill-1', kind: 'kill', at: Date.now() / 1000, caption: 'KILL CONFIRMED'}});
  await check('response uses full request timing instead of server timing', "document.getElementById('timer-value').textContent==='2.43s' && document.getElementById('timer-label').textContent==='Sage response'");
  await check('decision event survives a phase missed between polls', "document.getElementById('decision-card').classList.contains('punch') && document.getElementById('hud').classList.contains('phase-executing')");
  await check('fresh confirmed reaction visible', "!document.getElementById('reaction').hidden && document.getElementById('reaction-caption').textContent==='KILL CONFIRMED'");
  await screenshot('spectator-landscape.png');
  await wait(1100);
  await push({phase: 'executing', execution_label: 'Casting Smite', response_ms: 2432, decision_id: 'decision-1', decision_at: Date.now() / 1000, reaction: {id: 'kill-1', kind: 'kill', at: Date.now() / 1000, caption: 'KILL CONFIRMED'}});
  await check('repeat poll does not replay decision punch', "!document.getElementById('decision-card').classList.contains('punch')");
  await wait(1750);
  await check('repeat reaction ID folds on original deadline', "document.getElementById('reaction').hidden && !document.getElementById('folded-reaction').hidden");
  await push({reaction: {id: 'confusion-1', kind: 'confusion', at: Date.now() / 1000, caption: 'LOOKING FOR A TARGET'}});
  await check('confusion has its own reaction language', "document.getElementById('reaction').classList.contains('confusion') && document.getElementById('reaction-icon').textContent==='🔎'");
  await call('Emulation.setDeviceMetricsOverride', {width: 390, height: 844, deviceScaleFactor: 1, mobile: false});
  await wait(150);
  await check('small viewport keeps card and reaction inside viewport', "['decision-card','reaction'].every(id=>{const r=document.getElementById(id).getBoundingClientRect();return r.left>=0 && r.right<=innerWidth && r.top>=0 && r.bottom<=innerHeight;})");
  await screenshot('spectator-small.png');
  await push({state: 'PAUSED', phase: 'paused', reaction: {id: 'paused-event', kind: 'kill', at: Date.now() / 1000}});
  await check('pause clears reaction and all active motion', "document.getElementById('hud').classList.contains('is-idle') && document.getElementById('reaction').hidden && document.getElementById('timer-value').textContent==='—' && document.getElementById('hud').getAnimations({subtree:true}).length===0");
  await push({source_revision: 2, phase: 'decision', decision_id: 'new-source-decision', decision_at: Date.now() / 1000, reaction: {id: 'source-replay', kind: 'level', at: Date.now() / 1000}});
  await check('source replacement hydrates without replay', "document.getElementById('reaction').hidden && !document.getElementById('decision-card').classList.contains('punch')");
  await push({phase: 'decision', choice_kind: 'observation', choice_label: 'SEES LEVEL 3', decision_id: 'assessment-1', decision_at: Date.now() / 1000});
  const assessmentAttributed = await evaluate("document.getElementById('actor-label').textContent==='SAGE ASSESSED' && document.getElementById('choice-prefix').textContent==='ASSESSMENT'");
  await push({state: 'BLOCKED', phase: 'blocked', actor: 'system', request_elapsed: 3.4, choice_kind: 'observation', choice_label: 'SEES LEVEL 3', reaction: {id: 'blocked-kill', kind: 'kill', at: Date.now() / 1000}});
  await check('assessments are distinguished and blocked state shows no fake work', `${assessmentAttributed} && document.getElementById('state-label').textContent==='BLOCKED' && document.getElementById('choice-prefix').textContent==='LAST ASSESSMENT' && document.getElementById('timer-value').textContent==='—' && document.getElementById('reaction').hidden && document.querySelectorAll('[data-actor].active').length===0 && document.getElementById('hud').getAnimations({subtree:true}).length===0`);
  await push({state: 'BLOCKED', reason: 'navigation_no_action_selected'});
  await check('navigation wait identifies internal recovery without fake input', "document.getElementById('decision-title').textContent==='NAVIGATION STALLED' && document.getElementById('status-note').textContent==='No hunting action selected' && document.getElementById('state-label').textContent==='WAITING' && document.getElementById('timer-value').textContent==='—' && document.querySelectorAll('[data-actor].active').length===0");
  await screenshot('spectator-navigation-wait.png');
  await push({state: 'PAUSED', reason: 'clean_focus_lost'});
  await check('focus pause has its own cause instead of navigation failure', "document.getElementById('decision-title').textContent==='WOW NOT IN FOREGROUND' && document.getElementById('state-label').textContent==='PAUSED' && document.getElementById('status-note').textContent==='Waiting for WoW foreground'");
  await push({choice_label: '<img src=x onerror=alert(1)>', request_elapsed: null});
  await check('labels are text and unknown clock remains unknown', "document.getElementById('last-choice').children.length===0 && document.getElementById('last-choice').textContent.includes('<img') && document.getElementById('timer-value').textContent==='…'");
  await push({generated_at: Date.now() / 1000 - 10, reaction: {id: 'stale', kind: 'kill', at: Date.now() / 1000}});
  await check('old producer timestamp fails to unavailable', "document.getElementById('hud').classList.contains('phase-unavailable') && document.getElementById('reaction').hidden");
  await push({});
  await wait(5200);
  await check('delivery loss expires independently of native polling', "document.getElementById('hud').classList.contains('phase-unavailable') && document.getElementById('timer-value').textContent==='—'");
  assert.deepEqual(result.errors, [], 'no browser errors');
  assert.deepEqual(result.externalRequests, [], 'no external network requests');
} catch (error) {
  result.failure = String(error.stack || error);
  process.exitCode = 1;
} finally {
  socket?.close();
  chrome.kill('SIGTERM');
  await fs.writeFile(path.join(output, 'verification.json'), JSON.stringify(result, null, 2) + '\n');
  console.log(JSON.stringify(result, null, 2));
}
