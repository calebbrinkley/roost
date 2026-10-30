const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function fixture(fetch = async () => ({ok: true, json: async () => ({signed_in: false})}), globals = {}) {
  const nodes = new Map();
  function node() {
    const classes = new Set();
    return {
      value: '', textContent: '', disabled: false, open: false, dataset: {}, style: {}, children: [], events: {}, attributes: {},
      get options() { return this.children; },
      scrollTop: 0, scrollHeight: 100, clientHeight: 100,
      classList: { add(...names) { names.forEach(n => classes.add(n)); }, remove(...names) { names.forEach(n => classes.delete(n)); },
        contains(n) { return classes.has(n); }, toggle(n, on = !classes.has(n)) { on ? classes.add(n) : classes.delete(n); } },
      append(...children) { this.children.push(...children); }, replaceChildren(...children) { this.children = children; },
      remove() {}, setAttribute(name, value) { this.attributes[name] = String(value); }, getAttribute(name) { return this.attributes[name] ?? null; }, addEventListener(type, fn) { this.events[type] = fn; }, focus() {}, close() { this.open = false; },
      showModal() { this.open = true; }, querySelectorAll() { return []; }, closest() { return null; },
    };
  }
  const get = id => { if (!nodes.has(id)) nodes.set(id, node()); return nodes.get(id); };
  get('app').classList.add('hidden');
  const document = { getElementById: get, createElement: tag => Object.assign(node(), {tagName: String(tag).toUpperCase()}), createElementNS: node, createTextNode: text => ({textContent: text}),
    querySelectorAll: () => [], querySelector: () => get('main'), events: {}, addEventListener(name, fn) { this.events[name] = fn; } };
  const context = vm.createContext({ document, console, URL, setTimeout: () => 1, clearTimeout() {}, setInterval() {}, navigator: {},
    window: { roostTheme: { current: () => 'dark', collapseThinking: () => false }, addEventListener() {}, matchMedia: () => ({matches: false}), getSelection: () => '' },
    fetch, AbortSignal, URLSearchParams, Date, Math, atob, btoa, crypto: require('node:crypto').webcrypto, ...globals });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../roost/static/app.js'), 'utf8'), context);
  return {context, get, run: code => vm.runInContext(code, context)};
}

const tick = () => new Promise(resolve => setImmediate(resolve));

test('accepted message is not restored when refresh fails', async () => {
  const f = fixture(); await tick();
  f.get('message').value = 'hello';
  f.run(`state.current = 'one'; api = async () => ({session_id: 'one'});
    loadConversations = async () => { throw new Error('refresh failed'); }; loadMessages = async () => {};`);
  await f.run(`submitMessage({preventDefault() {}})`);
  assert.equal(f.get('message').value, '');
});

test('sending response does not switch away from a newly selected conversation', async () => {
  const f = fixture(); await tick(); f.get('message').value = 'hello';
  f.run(`state.current = 'one'; api = () => new Promise(resolve => { globalThis.finish = resolve; });
    loadConversations = async () => {}; loadMessages = async () => {};`);
  const sending = f.run(`submitMessage({preventDefault() {}})`);
  f.run(`state.current = 'two';`); f.get('message').value = 'next draft';
  f.run(`finish({session_id: 'one'});`); await sending;
  assert.equal(f.run('state.current'), 'two');
  assert.equal(f.get('message').value, 'next draft');
});

test('failed send preserves a draft typed while the request was pending', async () => {
  const f = fixture(); await tick(); f.get('message').value = 'first draft';
  f.run(`state.current = 'one'; api = () => new Promise((resolve, reject) => { globalThis.fail = reject; });
    loadMessages = async () => {};`);
  const sending = f.run(`submitMessage({preventDefault() {}})`);
  f.get('message').value = 'new draft'; f.run(`fail(new Error('offline'));`); await sending;
  assert.equal(f.get('message').value, 'new draft');
});

test('sign-out clears private state, drafts and open dialogs', async () => {
  const f = fixture(); await tick();
  f.run(`state.current = 'one'; state.conversations = [{id:'one'}]; state.jobs = [{id:'job'}];
    state.turn = {id:'one', working:true}; state.working = true; state.jobRuns = {job: [{preview:'private report'}]}; state.search = 'old';`);
  f.get('job-dialog').open = true; f.get('conversation-sheet').open = true; f.get('message').value = 'private draft';
  f.get('transcript').append({textContent:'private transcript'});
  f.run('signedOut()');
  assert.equal(f.run('state.current'), null); assert.equal(f.run('state.turn'), null); assert.equal(f.run('state.working'), false);
  assert.equal(f.run('state.conversations.length'), 0); assert.equal(f.run('state.jobs.length'), 0);
  assert.equal(f.get('message').value, ''); assert.equal(f.get('transcript').children.length, 0);
  assert.equal(f.get('job-dialog').open, false); assert.equal(f.get('conversation-sheet').open, false);
});

test('older transcript response cannot overwrite a newer poll', async () => {
  const f = fixture(); await tick();
  f.run(`state.current = 'one'; globalThis.requests = []; globalThis.rendered = '';
    api = path => path.endsWith('/approvals') ? Promise.resolve({approvals:[]}) : new Promise(resolve => requests.push(resolve)); renderMessages = rows => { rendered = rows[0].content; };`);
  const old = f.run('loadMessages()'); const latest = f.run('loadMessages()');
  f.run(`requests[1]({messages: [{content:'new'}]});`); await latest;
  f.run(`requests[0]({messages: [{content:'old'}]});`); await old;
  assert.equal(f.run('rendered'), 'new');
});

test('an IME confirmation Enter does not submit a message', async () => {
  const f = fixture(); await tick(); let submitted = 0;
  f.get('composer').requestSubmit = () => submitted++;
  f.get('message').events.keydown({key:'Enter', isComposing:true, preventDefault() {}});
  assert.equal(submitted, 0);
});

test('job history refreshes even when cached', async () => {
  const f = fixture(); await tick();
  f.run(`state.selectedJob = 'one'; state.jobRuns.one = []; globalThis.historyLoads = 0;
    api = async () => ({jobs: [{id:'one'}]}); renderJobs = () => {}; loadJobRuns = async () => { historyLoads++; };`);
  await f.run('loadJobs()');
  assert.equal(f.run('historyLoads'), 1);
});

test('job save ignores double submission', async () => {
  const f = fixture(); await tick();
  f.run(`globalThis.saves = 0; api = () => { saves++; return new Promise(resolve => { globalThis.finish = resolve; }); };
    loadJobs = async () => {}; selectJob = () => {};`);
  const first = f.run('saveJob({preventDefault() {}})');
  const second = f.run('saveJob({preventDefault() {}})');
  assert.equal(f.run('saves'), 1); f.run(`finish({id:'one'});`); await Promise.all([first,second]);
});

test('a response from before sign-out cannot repopulate conversations or show its error', async () => {
  const f = fixture(); await tick();
  f.context.fetch = () => new Promise(resolve => { f.context.finish = resolve; });
  const loading = f.run('loadConversations()');
  f.run('signedOut()');
  f.context.finish({ok:true,status:200,json:async () => ({sessions:[{id:'old',title:'private'}],total:1})});
  await assert.rejects(loading, /earlier sign-in/);
  assert.equal(f.run('state.conversations.length'),0);
  // A step that meets an error after its sign-in ended stays quiet.
  f.run(`report(new Error('old failure'), signInScope()); report(new Error('stale'), {alive: () => false});`);
  assert.equal(f.get('notice').textContent, 'old failure');
});

test('every field app.js writes to state is declared in a slice, so sign-out resets it or KEPT says why not', () => {
  const f = fixture();
  const source = require('node:fs').readFileSync(require('node:path').join(__dirname, '..', 'roost', 'static', 'app.js'), 'utf8');
  const written = new Set([...source.matchAll(/state\.([A-Za-z]+)\s*(?:=(?!=)|\+\+|\+=)/g)].map(m => m[1]));
  for (const block of source.matchAll(/Object\.assign\(state,\s*\{([^}]*)\}/g)) {
    for (const m of block[1].matchAll(/([A-Za-z]+):/g)) written.add(m[1]);
  }
  const declared = new Set(f.run('Object.keys({...KEPT(), ...freshSlices()})'));
  assert.ok(written.size > 40, 'the scan finds the writes');
  assert.deepEqual([...written].filter(name => !declared.has(name)), []);
  const kept = new Set(f.run('Object.keys(KEPT())'));
  assert.deepEqual(Array.from(f.run('Object.values(SLICES).flatMap(slice => Object.keys(slice()))')).filter(name => kept.has(name)), [],
    'a field is either reset or kept, not both');
});

test('an old unauthorized response cannot sign out a newer session', async () => {
  const f = fixture(); await tick();
  f.context.fetch = () => new Promise(resolve => { f.context.finish = resolve; });
  const loading = f.run('api("/jobs")');
  f.run(`signedOut(); show('app',true);`);
  f.context.finish({ok:false,status:401,json:async () => ({error:'old request'})});
  await assert.rejects(loading);
  assert.equal(f.get('app').classList.contains('hidden'),false);
});

test('job run failure offers a retry instead of endless loading', async () => {
  const f = fixture(); await tick();
  f.run(`state.selectedJob = 'one'; state.jobs = [{id:'one'}]; api = async () => { throw new Error('offline'); };`);
  await assert.rejects(f.run(`loadJobRuns('one')`));
  const text = n => (n.textContent || '') + n.children?.map(text).join('');
  assert.match(text(f.get('job-detail')), /could not load/);
  f.run(`api = async () => ({runs:[{preview:'new report'}]});`); await f.run(`loadJobRuns('one')`);
  assert.equal(f.run('state.jobRuns.one[0].preview'),'new report');
  assert.equal(f.run('state.jobRunsError.one'),undefined);
});

test('repeated Run now clicks submit only one action', async () => {
  const f = fixture(); await tick();
  f.run(`globalThis.actions = 0; api = () => { actions++; return new Promise(resolve => {globalThis.finish = resolve;}); };
    loadJobs = async () => {}; renderJobDetail = () => {};`);
  const first = f.run(`jobAction('one','trigger')`); const second = f.run(`jobAction('one','trigger')`);
  assert.equal(f.run('actions'),1); f.run(`finish({});`); await Promise.all([first,second]);
});

test('failed sign-out keeps the session visible and reports the error', async () => {
  const f = fixture(); await tick();
  f.run(`show('app',true); api = async () => {throw new Error('offline');};`);
  await f.run('signOut()');
  assert.equal(f.get('app').classList.contains('hidden'),false);
  assert.equal(f.get('notice').textContent,'offline');
  assert.equal(f.run('state.settingsBusy'), false);
});

test('ambiguous or invalid cron expressions are not described as a different schedule', async () => {
  const f = fixture(); await tick();
  for (const schedule of ['99 25 * * *','0 8 0 * *','0 8 1 * 0']) {
    f.context.schedule = schedule; assert.equal(f.run('scheduleLabel(schedule)'),schedule);
  }
  assert.equal(f.run(`scheduleLabel('0 8 * * 1-5')`),'Weekdays at 8:00 AM');
});

test('refreshing loaded pages after insertion leaves no gap and loading more advances correctly', async () => {
  const f = fixture(); await tick();
  // A list taller than the screen, scrolled to the top: nothing loads until asked.
  f.get('conversation-list').scrollHeight = 5000;
  f.run(`state.current = 'd'; state.conversations = ['a','b','c','d'].map(id => ({id}));
    api = async path => { const offset = Number(path.split('offset=')[1]);
      return {sessions:['new','a','b','c','d'].slice(offset,offset+2).map(id => ({id})), total:5}; };
    renderConversations = () => {};`);
  await f.run('loadConversations()');
  assert.deepEqual(Array.from(f.run('state.conversations.map(row => row.id)')),['new','a','b','c']);
  await f.run('loadConversations(true)');
  assert.deepEqual(Array.from(f.run('state.conversations.map(row => row.id)')),['new','a','b','c','d']);
});

test('saving a Job re-enables its Run now and Edit controls immediately', async () => {
  const f = fixture(); await tick();
  f.run(`api = async path => path === '/jobs' ? {jobs:[{id:'one'}]} : {id:'one',runs:[]};`);
  await f.run('saveJob({preventDefault() {}})');
  assert.equal(f.get('run-selected-job').disabled,false);
  assert.equal(f.get('edit-selected-job').disabled,false);
});

test('cancelled touch gesture cannot open deletion confirmation', async () => {
  const f = fixture(); await tick();
  f.context.row = f.get('test-row'); f.context.wrapper = f.get('test-wrapper');
  f.context.wrapper.clientWidth = 200;
  f.run(`phoneWidth.matches = true; globalThis.confirmations = 0;
    confirmDeleteConversation = () => confirmations++; swipeToDelete(wrapper,row,{id:'one'});`);
  const event = (type,x) => ({type,touches:[{clientX:x,clientY:0}],preventDefault() {}});
  f.context.row.events.touchstart(event('touchstart',200));
  f.context.row.events.touchmove(event('touchmove',40));
  f.context.row.events.touchcancel(event('touchcancel',40));
  assert.equal(f.run('confirmations'),0);
});

test('malformed transcript and Job rows do not crash the remaining valid content', async () => {
  const f = fixture(); await tick();
  f.run(`state.current = 'one'; renderMessages([null,{role:'user',content:'valid message'},{role:'assistant',content:{text:7}}]);`);
  const text = n => (n.textContent || '') + (n.children?.map(text).join('') || '');
  assert.match(text(f.get('transcript')),/valid message/);
  f.run(`api = async path => path === '/jobs' ? {jobs:[null,{id:'one',name:'valid job'}]} : {runs:[null,{preview:'valid run'}]};`);
  await f.run('loadJobs()');
  assert.match(text(f.get('sidebar-jobs-list')),/valid job/);
  assert.match(text(f.get('job-detail')),/valid run/);
});

test('transcript keeps updating when every poll overlaps the next one', async () => {
  const f = fixture(); await tick();
  f.run(`state.current = 'one'; globalThis.requests = []; globalThis.rendered = [];
    api = path => path.endsWith('/approvals') ? Promise.resolve({approvals:[]}) : new Promise(resolve => requests.push(resolve)); renderMessages = rows => { rendered.push(rows[0].content); };`);
  const loads = [f.run('loadMessages()')];
  for (let i = 0; i < 5; i++) {
    loads.push(f.run('loadMessages()'));
    f.run(`requests[${i}]({messages: [{content:'reply ${i}'}]});`); await loads[i];
  }
  assert.deepEqual(Array.from(f.run('rendered')), ['reply 0','reply 1','reply 2','reply 3','reply 4']);
  f.run(`requests[5]({messages: [{content:'reply 5'}]});`); await loads[5];
  assert.equal(f.run('rendered.at(-1)'), 'reply 5');
});

test('a transcript response from an earlier view or sign-in is discarded', async () => {
  const f = fixture(); await tick();
  f.run(`state.current = 'one'; globalThis.requests = []; globalThis.rendered = [];
    api = path => path.endsWith('/approvals') ? Promise.resolve({approvals:[]}) : new Promise(resolve => requests.push(resolve)); renderMessages = rows => { rendered.push(rows[0].content); };`);
  const beforeSwitch = f.run('loadMessages()');
  f.run(`state.viewEpoch++; state.current = 'two'; state.viewEpoch++; state.current = 'one';`);
  f.run(`requests[0]({messages: [{content:'old view'}]});`); await beforeSwitch;
  const beforeSignOut = f.run('loadMessages()');
  f.run(`signedOut(); state.current = 'one'; requests[1]({messages: [{content:'old session'}]});`); await beforeSignOut;
  assert.equal(f.run('rendered.length'), 0);
});

test('session expiry during a Job save leaves the reopened form usable', async () => {
  const f = fixture(); await tick();
  const button = f.get('save-job-button'); f.get('job-form').querySelectorAll = () => [button];
  f.context.fetch = async () => ({ok:false, status:401, json: async () => ({error:'expired'})});
  await f.run('saveJob({preventDefault() {}})');
  assert.equal(f.get('login').classList.contains('hidden'), false);
  f.context.fetch = async () => ({ok:true, json: async () => ({})});
  f.run('openJob()');
  assert.equal(f.get('job-dialog').open, true);
  assert.equal(button.disabled, false);
});

test('a late Job save from an old session cannot unlock a save in the new one', async () => {
  const f = fixture(); await tick();
  const button = f.get('save-job-button'); f.get('job-form').querySelectorAll = () => [button];
  f.context.fetch = () => new Promise(resolve => { f.context.finishOld = resolve; });
  const old = f.run('saveJob({preventDefault() {}})');
  f.run('signedOut(); openJob();');
  f.context.fetch = () => new Promise(resolve => { f.context.finishNew = resolve; });
  const current = f.run('saveJob({preventDefault() {}})');
  assert.equal(button.disabled, true);
  f.context.finishOld({ok:true, json: async () => ({id:'old'})}); await old;
  assert.equal(button.disabled, true); assert.equal(f.run('state.jobBusy'), true);
  f.run(`loadJobs = async () => {}; selectJob = () => {};`);
  f.context.finishNew({ok:true, json: async () => ({id:'new'})}); await current;
  assert.equal(button.disabled, false);
});

test('Load more after deleting a loaded conversation skips nothing', async () => {
  const f = fixture(); await tick();
  f.run(`globalThis.server = ['a','b','c','d'];
    api = async (path, options = {}) => {
      if (options.method === 'DELETE') { server = server.filter(id => !path.endsWith('/' + id)); return {}; }
      const offset = Number(path.split('offset=')[1]);
      return {sessions: server.slice(offset, offset + 2).map(id => ({id})), total: server.length};
    };
    renderConversations = () => {};`);
  await f.run('loadConversations()');
  f.run(`state.sheetItem = state.conversations[0];`);
  await f.run('deleteConversation()');
  await f.run('loadConversations(true)');
  assert.deepEqual(Array.from(f.run('state.conversations.map(row => row.id)')), ['b','c','d']);
  assert.equal(f.run('state.conversationsMore'), false);
});

test('a conversation refresh started before a deletion cannot bring the row back', async () => {
  const f = fixture(); await tick();
  f.run(`state.conversations = [{id:'a'},{id:'b'}]; state.conversationOffset = 2; globalThis.pending = [];
    api = (path, options = {}) => options.method === 'DELETE' ? Promise.resolve({}) : new Promise(resolve => pending.push(resolve));
    renderConversations = () => {};`);
  const refresh = f.run('loadConversations()');
  f.run(`state.sheetItem = {id:'a'};`); await f.run('deleteConversation()');
  f.run(`pending[0]({sessions:[{id:'a'},{id:'b'}], total:4});`); await refresh;
  assert.deepEqual(Array.from(f.run('state.conversations.map(row => row.id)')), ['b']);
  assert.equal(f.run('state.conversationOffset'), 1);
});

test('a created Job appears and stays even when the next list read does not have it yet', async () => {
  const f = fixture(); await tick();
  f.run(`api = async (path, options = {}) => {
      if (options.method === 'POST') return {id:'new', name:'Fresh', enabled:true};
      if (path === '/jobs') return {jobs: []};
      return {runs:[]};
    };`);
  await f.run('saveJob({preventDefault() {}})');
  assert.deepEqual(Array.from(f.run('state.jobs.map(job => job.id)')), ['new']);
  assert.equal(f.run('state.selectedJob'), 'new');
  await f.run('loadJobs()');
  assert.deepEqual(Array.from(f.run('state.jobs.map(job => job.id)')), ['new']);
});

test('pausing a Job flips its switch from the saved job, not a stale list', async () => {
  const f = fixture(); await tick();
  f.run(`state.jobs = [{id:'one', enabled:true}]; state.selectedJob = 'one';
    api = async (path, options = {}) => {
      if (path.endsWith('/pause')) return {id:'one', enabled:false};
      if (path === '/jobs') return {jobs: [{id:'one', enabled:true}]};
      return {runs:[]};
    };`);
  await f.run(`jobAction('one','pause')`);
  assert.equal(f.run('jobPaused(currentJob())'), true);
  assert.equal(f.run('state.jobBusy'), false);
});

test('Jobs share the sidebar search and + button with Messages', async () => {
  const f = fixture(); await tick();
  f.run(`state.jobs = [{id:'a', name:'Morning briefing'}, {id:'b', name:'Weekly review'}]; loadJobs = async () => {};
    globalThis.opened = 0; openJob = () => opened++;`);
  f.run(`setSection('jobs')`);
  assert.equal(f.get('sidebar-search').classList.contains('hidden'), false);
  assert.equal(f.get('search').placeholder, 'Search jobs');
  f.get('search').value = 'weekly'; f.get('search').events.input();
  const text = n => (n.textContent || '') + (n.children?.map(text).join('') || '');
  assert.match(text(f.get('sidebar-jobs-list')), /Weekly review/);
  assert.doesNotMatch(text(f.get('sidebar-jobs-list')), /Morning briefing/);
  f.get('sidebar-new').events.click();
  assert.equal(f.run('opened'), 1);
  f.run(`renderConversations = () => {}; setSection('messages')`);
  assert.equal(f.get('search').value, '');
  assert.equal(f.get('search').placeholder, 'Search conversations');
});

function settingsFixture(...args) {
  const f = fixture(...args);
  Object.assign(f.context.window.roostTheme, { mode: 'system', accent: 'forest', size: 'default',
    choice() { return this.mode; }, set(mode) { this.mode = mode; }, setAccent(name) { this.accent = name; },
    textSize() { return this.size; }, setTextSize(name) { this.size = name; } });
  return f;
}
const SETTINGS = "{agent_name:'', hermes_agent_name:'Kestrel', accent:'forest', accents:['forest','ocean','violet','rose','amber','slate']}";
const text = n => (n.textContent || '') + (n.children?.map(text).join('') || '');
const find = (n, test) => test(n) ? n : n.children?.map(c => find(c, test)).find(Boolean);

test('Settings opens on Appearance with no search field', async () => {
  const f = settingsFixture(); await tick();
  f.run(`api = async path => path === '/settings' ? ${SETTINGS} : {};`);
  f.run(`setSection('settings')`); await tick();
  assert.equal(f.get('sidebar-search').classList.contains('hidden'), true);
  assert.equal(f.get('sidebar-settings-list').classList.contains('hidden'), false);
  assert.equal(f.get('settings-title').textContent, 'Appearance');
  assert.match(text(f.get('settings-detail')), /System.*Light.*Dark/);
});

test('choosing an accent saves it for every device and applies it here', async () => {
  const f = settingsFixture(); await tick();
  f.run(`globalThis.sent = []; api = async (path, options = {}) => {
    if (options.method === 'PATCH') { sent.push(JSON.parse(options.body)); return {...${SETTINGS}, accent:'ocean'}; }
    return ${SETTINGS}; };`);
  f.run(`setSection('settings')`); await tick();
  const swatch = find(f.get('settings-detail'), n => n.className === 'swatch swatch-ocean');
  await swatch.events.click(); await tick();
  assert.deepEqual(JSON.parse(JSON.stringify(f.run('sent'))), [{accent:'ocean'}]);
  assert.equal(f.context.window.roostTheme.accent, 'ocean');
});

test('the custom accent previews while picking and saves the colour when the picker closes', async () => {
  const f = settingsFixture(); await tick();
  f.run(`globalThis.sent = []; api = async (path, options = {}) => {
    if (options.method === 'PATCH') { sent.push(JSON.parse(options.body)); return {...${SETTINGS}, accent:'#a1b2c3'}; }
    return ${SETTINGS}; };`);
  f.run(`setSection('settings')`); await tick();
  const picker = find(f.get('settings-detail'), n => n.type === 'color');
  picker.value = '#a1b2c3'; picker.events.input();
  assert.equal(f.context.window.roostTheme.accent, '#a1b2c3');
  assert.deepEqual(JSON.parse(JSON.stringify(f.run('sent'))), []);
  await picker.events.change(); await tick();
  assert.deepEqual(JSON.parse(JSON.stringify(f.run('sent'))), [{accent:'#a1b2c3'}]);
  const custom = find(f.get('settings-detail'), n => n.className === 'swatch swatch-custom chosen');
  assert.ok(custom, 'the saved custom colour shows as chosen');
});

test('delete all stays disabled until its own phrase is typed, then sends the confirmation', async () => {
  const f = settingsFixture(); await tick();
  f.run(`globalThis.calls = []; api = async (path, options = {}) => {
    if (options.method === 'DELETE') { calls.push([path, JSON.parse(options.body)]); return {deleted: 3}; }
    return ${SETTINGS}; };
    state.conversations = [{id:'a'}]; state.current = 'a'; renderConversations = () => {}; renderWelcome = () => {};`);
  f.run(`setSection('settings'); selectSettingsGroup('data')`); await tick();
  const input = find(f.get('settings-detail'), n => n.id === 'confirm-conversations');
  const button = find(f.get('settings-detail'), n => n.textContent === 'Delete all conversations' && n.className === 'danger-button');
  assert.equal(button.disabled, true);
  assert.equal(input.placeholder, 'Type “delete all conversations” to confirm');
  for (const wrong of ['DELETE', 'delete all jobs', 'delete all conversation']) {
    input.value = wrong; input.events.input();
    assert.equal(button.disabled, true, wrong);
  }
  input.value = '  Delete  all conversations '; input.events.input();
  assert.equal(button.disabled, false);
  await button.events.click(); await tick();
  assert.deepEqual(JSON.parse(JSON.stringify(f.run('calls'))), [['/conversations', {confirm:'DELETE'}]]);
  assert.equal(f.run('state.conversations.length'), 0); assert.equal(f.run('state.current'), null);
  assert.equal(f.get('notice').textContent, 'Deleted 3 conversations.');
});

test('saving an agent name applies the name the server now reports, with no second request', async () => {
  const f = settingsFixture(); await tick();
  f.run(`globalThis.paths = []; api = async (path, options = {}) => {
    paths.push(path);
    if (options.method === 'PATCH') return {...${SETTINGS}, agent_name: 'Robin', agent: 'Robin'};
    if (path === '/settings/soul') return {content: 'Be brief.'};
    return ${SETTINGS}; };`);
  f.run(`setSection('settings'); selectSettingsGroup('agent')`); await tick(); await tick();
  const input = find(f.get('settings-detail'), n => n.id === 'settings-agent-name');
  assert.equal(input.placeholder, 'Kestrel');
  assert.equal(find(f.get('settings-detail'), n => n.id === 'settings-soul').value, 'Be brief.');
  input.value = 'Robin'; input.events.input();
  await find(f.get('settings-detail'), n => n.textContent === 'Save name').events.click(); await tick();
  assert.equal(f.run('agent()'), 'Robin');
  assert.equal(f.run('paths').includes('/me'), false);
});

test('Settings applies the name, accent and time zone another device saved', async () => {
  const f = settingsFixture(); await tick();
  f.run(`api = async () => ({...${SETTINGS}, agent: 'Example Agent', accent: '#a1b2c3', time_zone: 'Asia/Tokyo'}); renderConversations = () => {};`);
  f.run(`setSection('settings')`); await tick();
  assert.deepEqual([f.run('agent()'), f.context.window.roostTheme.accent, f.run('state.timeZone')], ['Example Agent', '#a1b2c3', 'Asia/Tokyo']);
  assert.ok(find(f.get('settings-detail'), n => n.className === 'swatch swatch-custom chosen'), 'not a preset, so Custom is chosen');
});

test('times show in the chosen time zone, and the device zone when none is chosen', async () => {
  const f = settingsFixture(); await tick();
  f.run(`state.timeZone = 'Asia/Tokyo'`);
  // 20:00 UTC on Jan 5 is 05:00 on Jan 6 in Tokyo.
  assert.match(f.run(`messageTime('2020-01-05T20:00:00Z')`), /Jan 6, 2020, 5:00/);
  f.run(`state.timeZone = 'America/Los_Angeles'`);
  assert.match(f.run(`messageTime('2020-01-05T20:00:00Z')`), /Jan 5, 2020, 12:00/);
  f.run(`state.timeZone = 'Not/AZone'`);
  assert.notEqual(f.run(`messageTime('2020-01-05T20:00:00Z')`), '');
});


test('tapping an assistant reply toggles whole-reply token usage and TPS beside its timestamp', async () => {
  const f = fixture(); await tick();
  f.context.rows = [
    {id:1, role:'user', content:'Question', timestamp:1700000000, roost_usage:{input_tokens:999,output_tokens:999,model_seconds:1}},
    {id:2, role:'assistant', content:'Reply', timestamp:1700000020, roost_usage:{input_tokens:1200,output_tokens:240,model_seconds:6}}
  ];
  f.run(`state.current = 'one'; renderMessages(rows)`);
  const body = () => find(f.get('transcript'), n => n.classList?.contains('has-time') && text(n) === 'Reply');
  assert.doesNotMatch(text(f.get('transcript')), /TPS|1,200/);
  body().events.click({target:{closest:()=>null}});
  assert.match(text(f.get('transcript')), /1,200 in \/ 240 out · 40\.0 TPS/);
  const metadata = find(f.get('transcript'), n => n.className === 'reply-usage');
  assert.ok(text(metadata).startsWith(f.run('messageTime(rows[1].timestamp)') + ' · '));
  f.run('renderMessages(rows)');
  assert.match(text(f.get('transcript')), /40\.0 TPS/, 'polling preserves the revealed metadata');
  body().events.click({target:{closest:()=>null}});
  assert.doesNotMatch(text(f.get('transcript')), /TPS/);
  const own = find(f.get('transcript'), n => n.classList?.contains('has-time') && text(n) === 'Question');
  own.events.click({target:{closest:()=>null}});
  assert.doesNotMatch(text(f.get('transcript')), /TPS|999/, 'owner messages only reveal their timestamp');
});

test('reply metrics distinguish unavailable and zero values without guessing from text', async () => {
  const f = fixture(); await tick();
  assert.equal(f.run('replyUsage({})'), '— in / — out · — TPS');
  assert.equal(f.run('replyUsage({token_count:100,usage:{avg_tps:50}})'), '— in / — out · — TPS');
  assert.equal(f.run('replyUsage({roost_usage:{input_tokens:0,output_tokens:0,model_seconds:2}})'), '0 in / 0 out · 0.0 TPS');
  for (const seconds of ['6', 0, -1, NaN, Infinity]) {
    f.context.seconds = seconds;
    assert.match(f.run('replyUsage({roost_usage:{input_tokens:10,output_tokens:5,model_seconds:seconds}})'), /— TPS$/);
  }
  f.context.badUsage = {input_tokens:true,output_tokens:'40',model_seconds:4};
  assert.equal(f.run('replyUsage({roost_usage:badUsage})'), '— in / — out · — TPS');
});

test('the Settings tab opens Settings beside Messages and Jobs', async () => {
  const f = settingsFixture(); await tick();
  f.run(`api = async path => path === '/settings' ? ${SETTINGS} : {};`);
  f.get('nav-settings').events.click(); await tick();
  assert.equal(f.run('state.section'), 'settings');
  assert.equal(f.get('nav-settings').classList.contains('active'), true);
  assert.equal(f.get('nav-messages').classList.contains('active'), false);
  f.get('nav-messages').events.click();
  assert.equal(f.get('nav-settings').classList.contains('active'), false);
});

test('About shows the build, live stats and Hermes, or says Hermes is unreachable', async () => {
  const f = settingsFixture(); await tick();
  f.run(`globalThis.about = {roost:{version:'0.1.0', revision:'e2ebc26784892de5558fd5bb95f589a1d3fe62b4'},
      runtime:{python:'3.14.0', aiohttp:'3.14.4'},
      stats:{uptime_seconds:7260, cpu_percent:1.5, memory_bytes:52428800, memory_limit_bytes:null, process_memory_bytes:41943040,
             app_bytes:2097152, state_bytes:24576, disk_total_bytes:1e12, disk_free_bytes:4e11},
      hermes:{reachable:true, latency_ms:12, version:'2026.9.24', gateway_running:true, active_sessions:2}};
    api = async path => path === '/about' ? about : ${SETTINGS};`);
  f.run(`setSection('settings'); selectSettingsGroup('about')`); await tick(); await tick();
  const shown = text(f.get('settings-detail'));
  for (const part of [/0\.1\.0/, /e2ebc26/, /2h 1m/, /50 MB/, /Running/, /12 ms/, /Space Grotesk/, /calebbrinkley/,
                      /Roost on disk2\.0 MB ?app 2\.0 MB · data 24 KB/, /Free space373 GB ?of 931 GB on the shared drive/]) assert.match(shown, part);
  f.run(`about.hermes = {reachable:false}; loadAbout()`); await tick(); await tick();
  assert.match(text(f.get('settings-detail')), /Unreachable/);
});

test('choosing a time zone saves it and re-renders times', async () => {
  const f = settingsFixture(); await tick();
  f.run(`globalThis.sent = []; api = async (path, options = {}) => {
    if (options.method === 'PATCH') { sent.push(JSON.parse(options.body)); return {...${SETTINGS}, time_zone:'Asia/Tokyo'}; }
    return ${SETTINGS}; };`);
  f.run(`setSection('settings'); selectSettingsGroup('time')`); await tick();
  const select = find(f.get('settings-detail'), n => n.id === 'settings-time-zone');
  select.value = 'Asia/Tokyo'; await select.events.change(); await tick();
  assert.deepEqual(JSON.parse(JSON.stringify(f.run('sent'))), [{time_zone:'Asia/Tokyo'}]);
  assert.equal(f.run('state.timeZone'), 'Asia/Tokyo');
});

test('older conversations load near the end of the list, with no button', async () => {
  const f = fixture(); await tick();
  const list = f.get('conversation-list');
  Object.assign(list, {scrollHeight: 3000, clientHeight: 800, scrollTop: 0});
  f.run(`globalThis.offsets = []; state.conversations = [{id:'a'}]; state.conversationOffset = 100; state.conversationsMore = true;
    api = async path => { offsets.push(Number(path.split('offset=')[1])); return {sessions:[{id:'b'}], total:101}; };`);
  list.events.scroll(); await tick();
  assert.deepEqual(Array.from(f.run('offsets')), []);
  list.scrollTop = 1900; list.events.scroll(); await tick(); await tick();
  assert.deepEqual(Array.from(f.run('offsets')), [100]);
  assert.deepEqual(Array.from(f.run('state.conversations.map(row => row.id)')), ['a','b']);
  assert.equal(f.run('state.conversationsMore'), false);
  assert.equal(f.run('state.loadingMore'), false);
});

test('pulling the list down past the threshold refreshes it; a short pull or a sideways drag does not', async () => {
  const f = fixture(); await tick();
  const list = f.get('conversation-list');
  Object.assign(list, {scrollHeight: 3000, clientHeight: 800, scrollTop: 0});
  f.run(`phoneWidth.matches = true; globalThis.refreshes = 0; loadConversations = async () => { refreshes++; };
    globalThis.setTimeout = (fn) => { fn(); return 1; };`);
  const touch = (type, x, y) => ({type, touches:[{clientX:x, clientY:y}], changedTouches:[{clientX:x, clientY:y}], preventDefault() {}});
  const drag = async (dx, dy) => {
    list.events.touchstart(touch('touchstart', 100, 100));
    list.events.touchmove(touch('touchmove', 100 + dx / 4, 100 + dy / 4));
    list.events.touchmove(touch('touchmove', 100 + dx, 100 + dy));
    await list.events.touchend(touch('touchend', 100 + dx, 100 + dy)); await tick(); await tick();
  };
  await drag(0, 60);
  assert.equal(f.run('refreshes'), 0);
  await drag(160, 40);
  assert.equal(f.run('refreshes'), 0);
  await drag(0, 200);
  assert.equal(f.run('refreshes'), 1);
  assert.equal(f.get('pull-indicator').style.height, '0px');
  assert.equal(f.run('state.refreshing'), false);
});

test("Appearance sets this device's text size", async () => {
  const f = settingsFixture(); await tick();
  f.run(`api = async path => path === '/settings' ? ${SETTINGS} : {};`);
  f.run(`setSection('settings')`); await tick();
  const larger = find(f.get('settings-detail'), n => n.textContent === 'Larger' && n.className.startsWith('segment'));
  larger.events.click();
  assert.equal(f.context.window.roostTheme.size, 'larger');
  assert.equal(find(f.get('settings-detail'), n => n.textContent === 'Larger' && n.className.startsWith('segment')).className, 'segment active');
});

function loadTheme(f) {
  f.context.document.documentElement = {dataset: {}, style: {removeProperty() {}, setProperty() {}}};
  f.context.window.matchMedia = () => ({matches: false, addEventListener() {}});
  f.context.window.dispatchEvent = () => {};
  f.context.Event = class Event {};
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../roost/static/theme.js'), 'utf8'), f.context);
}

test('Appearance keeps thinking collapsed on this device across reloads', async () => {
  const stored = new Map(), localStorage = {getItem: key => stored.get(key),
    setItem: (key, value) => stored.set(key, value), removeItem: key => stored.delete(key)};
  const f = settingsFixture(undefined, {localStorage}); await tick(); loadTheme(f);
  f.run(`state.settings = ${SETTINGS}; state.settingsGroup = 'appearance'; renderSettings()`);
  const toggle = find(f.get('settings-detail'), n => n.className === 'job-toggle off');
  assert.equal(f.context.window.roostTheme.collapseThinking(), false);
  assert.match(text(f.get('settings-detail')), /Keep thinking collapsed/);
  toggle.events.click();
  assert.equal(toggle.className, 'job-toggle');
  assert.equal(stored.get('roost-collapse-thinking'), 'true');
  const reloaded = settingsFixture(undefined, {localStorage}); await tick(); loadTheme(reloaded);
  assert.equal(reloaded.context.window.roostTheme.collapseThinking(), true);
  reloaded.run(`state.settings = ${SETTINGS}; state.settingsGroup = 'appearance'; renderSettings()`);
  find(reloaded.get('settings-detail'), n => n.className === 'job-toggle').events.click();
  const reset = settingsFixture(undefined, {localStorage}); await tick(); loadTheme(reset);
  assert.equal(reset.context.window.roostTheme.collapseThinking(), false);
});

test('thinking preference still works when device storage is unavailable', async () => {
  const f = fixture(undefined, {localStorage: {getItem() { throw new Error('unavailable'); },
    setItem() { throw new Error('unavailable'); }, removeItem() { throw new Error('unavailable'); }}});
  await tick(); loadTheme(f);
  assert.equal(f.context.window.roostTheme.collapseThinking(), false);
  f.context.window.roostTheme.setCollapseThinking(true);
  assert.equal(f.context.window.roostTheme.collapseThinking(), true);
});

test('Settings › Account signs this device out', async () => {
  const f = settingsFixture(); await tick();
  f.run(`globalThis.calls = []; api = async (path, options = {}) => { calls.push(path); return path === '/settings' ? ${SETTINGS} : {}; };`);
  f.run(`show('app', true); setSection('settings'); selectSettingsGroup('account')`); await tick();
  const button = find(f.get('settings-detail'), n => n.textContent === 'Sign out' && n.className === 'secondary');
  await button.events.click(); await tick();
  assert.ok(f.run('calls').includes('/logout'));
  assert.equal(f.get('app').classList.contains('hidden'), true);
  assert.equal(f.get('login').classList.contains('hidden'), false);
});

test('table cells render italics, bold and code; code blocks stay literal', async () => {
  const f = fixture(); await tick();
  f.run(`globalThis.out = document.createElement('div');
    addMarkdown(out, '| Task | State |\\n|---|---|\\n| *Backup* | **done** |\\n| \`rg\` | _waiting_ |\\n\\n\`\`\`\\ndef f(*args, **kwargs): return a * b * c\\n\`\`\`');`);
  const tags = n => [n.tagName || '', ...(n.children || []).flatMap(tags)];
  const find = (n, tag) => (n.tagName === tag ? [n] : []).concat(...(n.children || []).map(c => find(c, tag)));
  const out = f.context.out;
  const cells = find(out, 'TD');
  assert.equal(find(cells[0], 'EM')[0].textContent, 'Backup');
  assert.equal(find(cells[1], 'STRONG')[0].textContent, 'done');
  assert.equal(find(cells[2], 'CODE')[0].textContent, 'rg');
  assert.equal(find(cells[3], 'EM')[0].textContent, 'waiting');
  const pre = find(out, 'PRE')[0];
  assert.equal(pre.children[0].textContent, 'def f(*args, **kwargs): return a * b * c');
  assert.equal(tags(pre).includes('EM'), false);
});

function passkeyFixture() {
  const f = settingsFixture();
  Object.assign(f.context.window, { PublicKeyCredential: function () {}, isSecureContext: true });
  const bytes = (...values) => Uint8Array.from(values).buffer;
  f.context.navigator.credentials = {
    get: async (request) => { f.context.requested = request.publicKey; return {
      rawId: bytes(1, 2, 3), response: { clientDataJSON: bytes(4), authenticatorData: bytes(5), signature: bytes(250, 251), userHandle: bytes(9) } }; },
    create: async (request) => { f.context.created = request.publicKey; return {
      rawId: bytes(7, 7), response: { clientDataJSON: bytes(8), attestationObject: bytes(255) } }; },
  };
  return f;
}

test('passkey sign-in sends the authenticator response and signs in', async () => {
  const f = passkeyFixture(); await tick();
  f.run(`globalThis.sent = []; signedIn = async () => { globalThis.done = true; };
    api = async (path, options = {}) => { sent.push([path, options.body && JSON.parse(options.body)]);
      return path === '/passkeys/login/options' ? {challenge:'AQID', rpId:'roost.example.invalid', userVerification:'required', timeout:1000} : {}; };`);
  await f.run('passkeySignIn()');
  assert.equal(f.context.requested.rpId, 'roost.example.invalid');
  assert.deepEqual(Array.from(new Uint8Array(f.context.requested.challenge)), [1, 2, 3]);
  const [path, body] = f.run('sent').at(-1);
  assert.equal(path, '/passkeys/login');
  assert.deepEqual(JSON.parse(JSON.stringify(body)), {id:'AQID', clientDataJSON:'BA', authenticatorData:'BQ', signature:'-vs', userHandle:'CQ'});
  assert.equal(f.run('globalThis.done'), true);
});

test('a cancelled passkey prompt says so and stays on the sign-in page', async () => {
  const f = passkeyFixture(); await tick();
  f.context.navigator.credentials.get = async () => { const error = new Error('x'); error.name = 'NotAllowedError'; throw error; };
  f.run(`globalThis.signedOutCalls = 0; const original = signedOut; signedOut = () => { signedOutCalls++; original(); };
    api = async () => ({challenge:'AQID', rpId:'r', userVerification:'required', timeout:1000});`);
  await f.run('passkeySignIn()');
  assert.equal(f.get('login-error').textContent, 'Passkey sign-in was cancelled or timed out.');
  assert.equal(f.run('signedOutCalls'), 0);
});

test('Settings › Account adds a named passkey and removes one after confirming', async () => {
  const f = passkeyFixture(); await tick();
  f.run(`globalThis.sent = []; globalThis.listed = [{id:'old', name:'Mac', created:1760000000, last_used:null}];
    api = async (path, options = {}) => {
      sent.push([options.method || 'GET', path, options.body && JSON.parse(options.body)]);
      if (path === '/settings') return ${SETTINGS};
      if (path === '/passkeys') return options.method === 'POST' ? {id:'Bwc', name:'iPhone'} : {passkeys: listed};
      if (path === '/passkeys/register/options') return {challenge:'AQID', rp:{id:'r', name:'Roost'}, user:{id:'CQ', name:'o', displayName:'o'},
        pubKeyCredParams:[], authenticatorSelection:{}, attestation:'none', excludeCredentials:[{type:'public-key', id:'AQ'}], timeout:1000};
      return {ok:true}; };`);
  f.run(`setSection('settings'); selectSettingsGroup('account')`); await tick(); await tick(); await tick();
  assert.match(text(f.get('settings-detail')), /Mac/);
  const name = find(f.get('settings-detail'), n => n.id === 'passkey-name');
  name.value = 'iPhone'; name.events.input();
  await find(f.get('settings-detail'), n => n.textContent === 'Add a passkey').events.click(); await tick();
  assert.deepEqual(Array.from(new Uint8Array(f.context.created.user.id)), [9]);
  assert.deepEqual(Array.from(new Uint8Array(f.context.created.excludeCredentials[0].id)), [1]);
  const added = f.run('sent').find(([method, path]) => method === 'POST' && path === '/passkeys');
  assert.deepEqual(JSON.parse(JSON.stringify(added[2])), {name:'iPhone', id:'Bwc', clientDataJSON:'CA', attestationObject:'_w'});
  const remove = () => find(f.get('settings-detail'), n => /^(Remove|Confirm remove)$/.test(n.textContent));
  remove().events.click();
  assert.equal(remove().textContent, 'Confirm remove');
  await remove().events.click(); await tick();
  assert.ok(f.run('sent').some(([method, path, body]) => method === 'DELETE' && path === '/passkeys/old' && body.confirm === true));
});

test('an unreachable server at start shows Connecting and retries, never a blank page or a sign-out', async () => {
  let calls = 0, respond, answer = () => Promise.reject(new TypeError('Load failed'));
  const f = fixture(() => { calls++; return answer(); }); await tick();
  assert.equal(f.run('state.authEpoch'), 0);
  assert.equal(f.get('connecting').classList.contains('hidden'), false);
  assert.match(f.get('connecting').textContent, /Retrying/);
  // A resume while a check is in flight does not start a second one.
  answer = () => new Promise(resolve => { respond = resolve; });
  f.run('start(); start();'); assert.equal(calls, 2);
  respond({ok: true, json: async () => ({signed_in: false})}); await tick(); await tick();
  assert.equal(f.get('connecting').classList.contains('hidden'), true);
  assert.equal(f.run('state.authEpoch'), 1);
  f.run('start();'); assert.equal(calls, 2);
});

test('the thinking indicator follows the server, and shows at once for a message just sent', async () => {
  const f = fixture(); await tick();
  const now = Date.now() / 1000, thinking = () => Boolean(find(f.get('transcript'), n => n.className === 'typing-row'));
  const user = {id: 1, role: 'user', content: 'check the logs', timestamp: now};
  f.context.rows = [];
  // Sent from this device; the server has not answered since.
  f.run(`state.current = 'one'; state.turn = {id: 'one', working: false, at: Date.now() - 1000};
    state.sentMessages = [{id: 'one', text: 'check the logs', since: Date.now(), after: 0, key: 'sent-1'}]; renderMessages(rows)`);
  assert.equal(thinking(), true, 'unsaved and not yet reported on');
  f.context.rows = [user, {id: 2, role: 'tool', content: 'ok', timestamp: now}];
  f.run(`state.turn = {id: 'one', working: true, at: Date.now()}; renderMessages(rows)`);
  assert.equal(thinking(), true);
  assert.equal(f.run('state.sentMessages.length'), 0, 'the saved row replaced the local one');
  f.context.rows.push({id: 3, role: 'assistant', content: 'All clear.', timestamp: now});
  f.run(`state.turn = {id: 'one', working: false, at: Date.now()}; renderMessages(rows)`);
  assert.equal(thinking(), false);
});

test('an opened conversation shows the indicator exactly when the server says the agent is working', async () => {
  const f = fixture(); await tick();
  const thinking = () => Boolean(find(f.get('transcript'), n => n.className === 'typing-row'));
  f.context.rows = [{id: 1, role: 'user', content: 'hi', timestamp: Date.now() / 1000}];
  for (const working of [true, false]) {
    f.context.working = working;
    f.run(`state.current = 'one'; state.turn = {id: 'one', working, at: Date.now()}; renderMessages(rows)`);
    assert.equal(thinking(), working);
  }
  f.run(`state.turn = {id: 'other', working: true, at: Date.now()}; renderMessages(rows)`);
  assert.equal(thinking(), false, "another conversation's answer does not apply here");
});

test('conversationView places the live turn by position, even beside identical messages', () => {
  const f = fixture();
  const rows = [{id: 1, role: 'user', content: 'ok'}, {id: 2, role: 'assistant', content: 'First answer.'},
    {id: 3, role: 'user', content: 'ok'}, {id: 4, role: 'assistant', content: 'Step.', tool_calls: [{id: 'a'}]}];
  f.context.rows = rows;
  const view = f.run(`conversationView({rows, sent: [], current: 'one', server: {working: true, after: 2, started: 7},
    live: {id: 'one', user: 'ok', started: 7, thoughts: '', text: 'Writing', done: false}})`);
  assert.equal(view.liveAfter.id, 3, 'the second "ok", not the first');
  assert.deepEqual(Array.from(view.rows.map(row => row.id)), [1, 2, 3], "the live turn's saved steps give way to the stream");
  assert.equal(view.after, 2);
  assert.equal(view.working, true);
  const stale = f.run(`conversationView({rows, sent: [], current: 'one', server: {working: true, after: 0, started: 6},
    live: {id: 'one', user: 'ok', started: 7, thoughts: '', text: '', done: false}})`);
  assert.equal(stale.after, null, "a response about an earlier turn doesn't place this one");
  assert.equal(stale.liveAfter.content, 'ok');
  assert.equal(stale.rows.length, 5, 'shown after the transcript until the server answers');
});

test('conversationView retires a finished live turn once it is saved or the server says it is over', () => {
  const f = fixture();
  f.context.rows = [{id: 1, role: 'user', content: 'hi'}, {id: 2, role: 'tool', content: 'ok'}];
  const live = "{id: 'one', user: 'hi', started: 7, thoughts: '', text: 'Hello.', done: true, after: 0}";
  assert.ok(f.run(`conversationView({rows, sent: [], current: 'one', server: {working: true}, live: ${live}})`).live,
    'done streaming, reply not saved yet: keep showing it');
  assert.equal(f.run(`conversationView({rows, sent: [], current: 'one', server: {working: false}, live: ${live}})`).live, null);
  f.context.rows.push({id: 3, role: 'assistant', content: 'Hello.'});
  assert.equal(f.run(`conversationView({rows, sent: [], current: 'one', server: {working: true}, live: ${live}})`).live, null);
});

test('the working label never says typing', async () => {
  const f = fixture(); await tick();
  assert.equal(f.run('WORKING_VERBS').includes('typing'), false);
  for (let i = 0; i < 50; i++) assert.notEqual(f.run('nextWorkingVerb(); state.workingVerb'), 'typing');
});

test('thoughts from every step fold, collapsed, into one block above the reply they led to', async () => {
  const f = fixture(); await tick();
  const now = Date.now() / 1000;
  f.context.rows = [
    {id: 1, role: 'user', content: 'check the logs', timestamp: now},
    {id: 2, role: 'assistant', content: '', reasoning: 'Read the log first.', tool_calls: [{id: 'a'}], timestamp: now},
    {id: 3, role: 'tool', content: 'ok', timestamp: now},
    {id: 4, role: 'assistant', content: 'All clear.', reasoning_content: 'Nothing failed.', timestamp: now},
    {id: 5, role: 'assistant', content: 'Anything else?', reasoning: 'hidden by Hermes', display_reasoning: '', timestamp: now}];
  f.run(`state.current = 'one'; renderMessages(rows)`);
  const kids = f.get('transcript').children, blocks = kids.filter(n => n.className === 'thoughts');
  assert.equal(blocks.length, 1);
  assert.equal(blocks[0].open, false);
  assert.match(text(blocks[0]), /Read the log first\.[\s\S]*Nothing failed\./);
  assert.equal(kids[kids.indexOf(blocks[0]) + 1].className, 'message from-agent');
  assert.match(text(kids[kids.indexOf(blocks[0]) + 1]), /All clear/);
});

test('an opened thoughts block stays open across polls, and a turn in progress shows its thoughts so far', async () => {
  const f = fixture(); await tick();
  const now = Date.now() / 1000, block = () => f.get('transcript').children.find(n => n.className === 'thoughts');
  f.context.rows = [{id: 1, role: 'user', content: 'hi', timestamp: now},
    {id: 2, role: 'assistant', content: '', reasoning: 'Planning.', tool_calls: [{id: 'a'}], timestamp: now}];
  f.run(`state.current = 'one'; state.turn = {id: 'one', working: true, at: Date.now()}; renderMessages(rows)`);
  const kids = f.get('transcript').children;
  assert.equal(kids.indexOf(block()), kids.findIndex(n => n.className === 'typing-row') - 1);
  block().open = true; block().events.toggle();
  f.context.rows.push({id: 3, role: 'assistant', content: 'Hello.', timestamp: now});
  f.run('renderMessages(rows)');
  assert.equal(block().open, true);
  f.run(`state.current = 'two'; renderMessages(rows)`);
  assert.equal(block().open, false);
});

const pushId = endpoint => require('node:crypto').createHash('sha256').update(endpoint).digest('hex').slice(0, 24);

function pushBrowser() {
  const calls = {permission: 0, subscribe: [], unsubscribed: 0};
  const subscription = {endpoint: 'https://web.push.apple.com/device', unsubscribe: async () => { calls.unsubscribed++; return true; },
    toJSON() { return {endpoint: this.endpoint, keys: {p256dh: 'BA', auth: 'AA'}}; }};
  let current = null;
  const registration = {pushManager: {getSubscription: async () => current,
    subscribe: async options => { calls.subscribe.push(options); current = subscription; return subscription; }}};
  const globals = {isSecureContext: true, PushManager: function () {}, crypto: require('node:crypto').webcrypto, TextEncoder,
    Notification: {permission: 'default', requestPermission: async () => { calls.permission++; return 'granted'; }},
    navigator: {userAgent: 'Mozilla/5.0 (iPhone)', serviceWorker: {register: async () => registration, ready: Promise.resolve(registration), addEventListener() {}}}};
  return {calls, globals, subscription};
}

test('Notifications turns on for this device from the tap, then marks it and can send a test', async () => {
  const browser = pushBrowser();
  const f = settingsFixture(undefined, browser.globals); await tick();
  const key = Buffer.concat([Buffer.from([4]), Buffer.alloc(64, 1)]).toString('base64url');
  f.context.devices = [];
  f.run(`globalThis.sent = []; api = async (path, options = {}) => {
    sent.push([options.method || 'GET', path, options.body && JSON.parse(options.body)]);
    if (path === '/settings') return ${SETTINGS};
    if (path === '/push') return {public_key: '${key}', devices};
    if (path === '/push/devices') { devices.push({id: '${pushId('https://web.push.apple.com/device')}', name: 'iPhone', created: 1, last_sent: null}); return {}; }
    return {};
  };`);
  f.run(`setSection('settings')`); await tick();
  await f.run(`selectSettingsGroup('notifications'); loadPush()`); await tick();
  assert.match(text(f.get('settings-detail')), /Notifications are off for this device/);
  await find(f.get('settings-detail'), n => n.textContent === 'Turn on').events.click(); await tick(); await tick();
  assert.equal(browser.calls.permission, 1);
  assert.equal(browser.calls.subscribe[0].userVisibleOnly, true);
  assert.equal(browser.calls.subscribe[0].applicationServerKey.byteLength, 65);
  const added = f.run('sent').find(([method, path]) => method === 'POST' && path === '/push/devices');
  assert.deepEqual(JSON.parse(JSON.stringify(added[2])), {subscription: browser.subscription.toJSON(), name: 'iPhone'});
  assert.match(text(f.get('settings-detail')), /Notifications are on for this device[\s\S]*iPhone \(this device\)/);
  await find(f.get('settings-detail'), n => n.textContent === 'Send a test').events.click(); await tick();
  assert.ok(f.run('sent').some(([method, path, body]) => method === 'POST' && path === '/push/test' && body.device === pushId(browser.subscription.endpoint)));
});

test('an iPhone outside the Home Screen is told how to get notifications, and other devices can be removed', async () => {
  const f = settingsFixture(undefined, {navigator: {userAgent: 'Mozilla/5.0 (iPhone)'}}); await tick();
  f.run(`globalThis.sent = []; api = async (path, options = {}) => {
    sent.push([options.method || 'GET', path, options.body && JSON.parse(options.body)]);
    if (path === '/settings') return ${SETTINGS};
    return path === '/push' ? {public_key: 'BA', devices: [{id: 'mac', name: 'Mac', created: 1, last_sent: 2}]} : {};
  };`);
  f.run(`setSection('settings')`); await tick();
  await f.run(`selectSettingsGroup('notifications'); loadPush()`); await tick();
  assert.match(text(f.get('settings-detail')), /Add to Home Screen/);
  assert.equal(find(f.get('settings-detail'), n => n.textContent === 'Turn on'), undefined);
  const remove = () => find(f.get('settings-detail'), n => /^(Remove|Confirm remove)$/.test(n.textContent));
  remove().events.click();
  assert.equal(remove().textContent, 'Confirm remove');
  await remove().events.click(); await tick();
  assert.ok(f.run('sent').some(([method, path, body]) => method === 'DELETE' && path === '/push/devices/mac' && body.confirm === true));
});

test('presence reports the conversation on screen, and nothing when hidden or back at the list on a phone', async () => {
  const posts = [];
  const f = fixture(async (url, options = {}) => {
    if (url === '/api/presence') posts.push(JSON.parse(options.body).conversation);
    return {ok: true, json: async () => ({signed_in: false})};
  }); await tick();
  f.run(`show('app', true); state.section = 'messages'; state.current = 'chat:one';`);
  f.context.document.visibilityState = 'visible'; f.run('reportPresence()');
  f.context.document.visibilityState = 'hidden'; f.run('reportPresence()');
  f.context.document.visibilityState = 'visible';
  f.run(`phoneWidth.matches = true; $('app').classList.remove('chat-open'); reportPresence();`);
  f.run(`$('app').classList.add('chat-open'); reportPresence();`);
  assert.deepEqual(posts, ['chat:one', null, null, 'chat:one']);
});

test('a tapped notification opens its conversation after sign-in', async () => {
  const f = fixture(undefined, {location: {search: '?c=chat%3Aone'}, history: {replaceState(state, title, url) { this.url = url; }}}); await tick();
  f.run(`globalThis.opened = []; api = async () => ({sessions: []}); selectConversation = id => opened.push(id);`);
  await f.run('signedIn()');
  assert.deepEqual(Array.from(f.run('opened')), ['chat:one']);
  assert.equal(f.context.history.url, '/');
});

function eventSources() {
  const opened = [];
  class EventSource {
    constructor(url) { this.url = url; this.readyState = 1; this.closed = false; opened.push(this); }
    close() { this.closed = true; this.readyState = 2; }
    send(data) { this.onmessage({data: JSON.stringify(data)}); }
  }
  return {opened, globals: {EventSource}};
}

test('a reply streams its thoughts, then its text, in place of the steps Hermes has saved so far', async () => {
  const {opened, globals} = eventSources();
  const f = fixture(undefined, globals); await tick();
  const now = Date.now() / 1000, kids = () => f.get('transcript').children;
  const message = n => n.className === 'message from-agent';
  f.get('message').value = 'check the logs';
  f.run(`state.current = 'one'; api = async () => ({session_id: 'one'}); loadConversations = async () => {};
    globalThis.loads = 0; loadMessages = async () => { loads++; };`);
  await f.run(`submitMessage({preventDefault() {}})`);
  assert.equal(opened.length, 1);
  assert.equal(opened[0].url, '/api/conversations/one/stream');
  const user = {id: 1, role: 'user', content: 'check the logs', timestamp: now};
  const interim = {id: 2, role: 'assistant', content: 'Let me look.', reasoning: 'Read the log.', tool_calls: [{id: 'a'}], timestamp: now};
  f.context.rows = [user, interim];
  // As loadMessages would record it: the server is following the turn that started at 1, after row 0.
  f.run(`state.turn = {id: 'one', working: true, after: 0, started: 1, at: Date.now()}; renderMessages(rows)`);
  opened[0].send({reset: true, user: 'check the logs', started: 1, thoughts: 'Read the log.'});
  f.run('renderLive()');
  const thoughts = kids().find(n => n.className === 'thoughts');
  assert.equal(thoughts.open, true);
  assert.match(text(thoughts), /Read the log\./);
  assert.equal(kids().find(message).classList.contains('hidden'), true);
  assert.equal(kids().filter(n => n.className === 'thoughts').length, 1, 'saved steps are not shown twice');
  opened[0].send({text: 'Let me look.\n\nAll clear.'});
  f.run('renderLive()');
  assert.match(text(kids().find(message)), /All clear\./);
  assert.ok(find(f.get('transcript'), n => n.className === 'typing-row'));
  opened[0].send({done: true});
  assert.equal(opened[0].closed, true);
  assert.equal(f.run('loads'), 2, 'the end of the stream reloads the transcript');
  f.context.rows = [user, interim, {id: 3, role: 'tool', content: 'ok', timestamp: now},
    {id: 4, role: 'assistant', content: 'All clear.', reasoning: 'Nothing failed.', timestamp: now}];
  f.run(`state.turn = {id: 'one', working: false, after: 0, started: null, at: Date.now()}; renderMessages(rows)`);
  assert.equal(f.run('state.live'), null);
  assert.equal(kids().find(n => n.className === 'thoughts').open, false);
  assert.equal(find(f.get('transcript'), n => n.className === 'typing-row'), undefined);
});

test('collapsed live thoughts respect manual toggles through updates, rebuilds and reconnects, then reset next turn', async () => {
  const {opened, globals} = eventSources();
  const f = fixture(undefined, globals); await tick(); loadTheme(f);
  f.context.window.roostTheme.setCollapseThinking(true);
  f.run(`state.current = 'one'; openStream('one'); renderMessages([])`);
  const block = () => f.get('transcript').children.find(n => n.className === 'thoughts');
  opened[0].send({reset: true, user: 'hello', started: 1, thoughts: 'Thinking'});
  f.run('renderLive()');
  assert.equal(block().open, false);
  block().open = true; block().events.toggle();
  opened[0].send({thoughts: ' more', text: 'Reply'});
  f.run('renderLive()');
  assert.equal(block().open, true);
  f.run(`state.messageSignature = null; renderMessages([])`);
  assert.equal(block().open, true, 'a transcript rebuild preserves the manual choice');
  opened[0].send({reset: true, user: 'hello', started: 1, thoughts: 'Thinking more'});
  f.run('renderLive()');
  assert.equal(block().open, true, 'reconnecting the same turn preserves the manual choice');
  opened[0].send({reset: true, user: 'again', started: 2, thoughts: 'New thinking'});
  f.run('renderLive()');
  assert.equal(block().open, false, 'each new turn starts collapsed');
  assert.equal(f.run(`thoughtsBlock(['Saved thinking'], 'saved').open`), false);
});

test('a reply being written shows the message it answers before Hermes has saved it', async () => {
  const {opened, globals} = eventSources();
  const f = fixture(undefined, globals); await tick();
  f.run(`state.current = 'one'; openStream('one'); renderMessages([])`);
  opened[0].send({reset: true, user: 'hello there', started: 1});
  f.run('renderLive()');
  const yours = f.get('transcript').children.find(n => n.className === 'message from-you');
  assert.match(text(yours), /hello there/);
});

test('a refused stream hands the turn back to the transcript, and leaving a conversation closes it', async () => {
  const {opened, globals} = eventSources();
  const f = fixture(undefined, globals); await tick();
  f.run(`state.current = 'one'; loadMessages = async () => {}; openStream('one')`);
  opened[0].send({reset: true, user: 'hi', started: 1, text: 'Hel'});
  opened[0].readyState = 2; opened[0].onerror();
  assert.equal(f.run('state.live.done'), true);
  assert.equal(f.run('state.stream'), null);
  f.run(`openStream('one')`);
  f.run(`api = async () => ({messages: []}); selectConversation('two')`);
  assert.equal(opened[1].closed, true);
  assert.equal(f.run('state.live'), null);
});

test('a message that carried instructions shows as typed, in the transcript and the list', async () => {
  const f = fixture(); await tick();
  const begin = f.run('INSTRUCTIONS_BEGIN'), end = f.run('INSTRUCTIONS_END');
  const sent = `${begin}\nAnswer in French.\n${end}\n\nhello there`;
  f.context.rows = [{role: 'user', content: sent, timestamp: Date.now() / 1000}, {role: 'assistant', content: 'Bonjour.', timestamp: Date.now() / 1000}];
  f.run(`state.current = 'one'; renderMessages(rows)`);
  const transcript = text(f.get('transcript'));
  assert.match(transcript, /hello there/);
  assert.doesNotMatch(transcript, /Answer in French/);
  assert.equal(f.run(`withoutInstructions(${JSON.stringify(sent.slice(0, 40))})`), '', 'a preview cut inside the instructions');
  assert.equal(f.run(`sessionName({preview: ${JSON.stringify(sent)}})`), 'hello there');
  assert.equal(f.run(`withoutInstructions(${JSON.stringify(sent.replaceAll('\n', ' '))})`), 'hello there', 'a preview flattened to one line');
  assert.equal(f.run(`withoutInstructions('Write [Instructions for this conversation] as a heading')`), 'Write [Instructions for this conversation] as a heading');
});

test('the gear opens this conversation\'s menu: instructions load and save, and Download points at the export', async () => {
  const f = fixture(); await tick();
  f.run(`state.current = 'chat:one'; state.conversations = [{id: 'chat:one', title: 'Plans'}]; globalThis.calls = [];
    api = async (path, options = {}) => { calls.push([path, options.method || 'GET', options.body]);
      return options.method === 'PUT' ? JSON.parse(options.body) : {instructions: 'Be brief.'}; };`);
  f.context.rows = [{role: 'user', content: 'hi'}];
  f.run('renderMessages(rows)');
  assert.equal(f.get('chat-settings').classList.contains('hidden'), false);
  f.get('chat-settings').events.click();
  assert.equal(f.get('conversation-sheet').open, true);
  assert.equal(f.get('sheet-title').textContent, 'Plans');
  assert.equal(f.get('sheet-export').href, '/api/conversations/chat%3Aone/export');
  await f.get('sheet-instructions').events.click();
  assert.equal(f.get('sheet-instructions-text').value, 'Be brief.');
  assert.equal(f.get('sheet-instructions-text').disabled, false);
  f.get('sheet-instructions-text').value = 'Answer in French.';
  await f.get('sheet-instructions-form').events.submit({preventDefault() {}});
  assert.deepEqual(JSON.parse(JSON.stringify(f.run('calls').at(-1))), ['/conversations/chat%3Aone/settings', 'PUT', '{"instructions":"Answer in French."}']);
  assert.equal(f.get('conversation-sheet').open, false);
  f.run('renderWelcome()');
  assert.equal(f.get('chat-settings').classList.contains('hidden'), true);
});

test('a tapped notification opens its job, or the message the agent sent', async () => {
  const f = fixture(); await tick();
  f.run(`globalThis.calls = []; api = async path => { calls.push(path);
    return path === '/jobs' ? {jobs: [{id: 'job_7', name: 'Morning brief'}]} : {id: 'd1', title: '', text: 'Dinner is at **7**.', created: 1760000000}; };
    loadJobRuns = async () => {};`);
  await f.run(`openLink('?j=job_7')`); await tick();
  assert.equal(f.run('state.section'), 'jobs');
  assert.equal(f.run('state.selectedJob'), 'job_7');
  await f.run(`openLink('?d=d1')`); await tick();
  assert.equal(f.get('delivery-sheet').open, true);
  assert.match(text(f.get('delivery-text')), /Dinner is at/);
  assert.equal(f.get('delivery-title').textContent, f.run('agent()'));
});

test('Notifications shows how to connect Hermes, with the token, and replaces the token after a confirm', async () => {
  const f = fixture(); await tick();
  f.run(`state.settings = {}; state.settingsGroup = 'notifications'; state.push = {devices: [], mine: null, public_key: ''};
    state.hermesLink = {token: 'tok-1', url: 'https://roost.example.invalid', last_delivery: null};
    globalThis.calls = []; api = async (path, options = {}) => { calls.push([path, options.body]);
      return {token: 'tok-2', url: 'https://roost.example.invalid', last_delivery: null}; };`);
  f.run('renderSettings()');
  const detail = text(f.get('settings-detail'));
  assert.match(detail, /ROOST_URL=https:\/\/roost\.example\.invalid\nROOST_DELIVERY_TOKEN=tok-1\nROOST_HOME_CHANNEL=roost/);
  assert.match(detail, /hermes plugins enable roost/);
  assert.match(detail, /Nothing received from Hermes yet/);
  const button = () => find(f.get('settings-detail'), n => /token/i.test(n.textContent || '') && n.events?.click);
  button().events.click();
  assert.equal(button().textContent, 'Confirm new token');
  await button().events.click(); await tick();
  assert.deepEqual(JSON.parse(JSON.stringify(f.run('calls').at(-1))), ['/hermes/token', '{"confirm":true}']);
  assert.equal(f.run('state.hermesLink.token'), 'tok-2');
});

function voiceBrowser() {
  const players = [];
  class Audio { constructor() { this.src = ''; this.events = {}; players.push(this); }
    addEventListener(type, fn) { this.events[type] = fn; } play() { this.played = this.src; return Promise.resolve(); } pause() { this.paused = true; } }
  const recorders = [];
  class MediaRecorder { constructor(stream, options) { this.mimeType = options?.mimeType || ''; this.state = 'inactive'; this.events = {}; recorders.push(this); }
    static isTypeSupported(type) { return type === 'audio/mp4'; }
    addEventListener(type, fn) { this.events[type] = fn; }
    start() { this.state = 'recording'; }
    stop() { this.state = 'inactive'; this.events.dataavailable({data: {size: 3, type: 'audio/mp4'}}); this.events.stop(); } }
  class FileReader { readAsDataURL() { this.result = 'data:audio/mp4;base64,AAAA'; this.onload(); } }
  const stream = {getTracks: () => [{stop() { stream.stopped = true; }}]};
  const globals = {Audio, MediaRecorder, FileReader, clearInterval() {}, Blob: class { constructor(parts, options) { this.type = options.type; } },
    navigator: {mediaDevices: {getUserMedia: async () => stream}}};
  return {players, recorders, stream, globals};
}

test('every reply has a play button that reads it aloud, and a second tap stops it', async () => {
  const {players, globals} = voiceBrowser();
  const f = fixture(undefined, globals); await tick();
  f.context.rows = [{id: 1, role: 'user', content: 'hi'}, {id: 2, role: 'assistant', content: 'Hello.'}];
  f.run(`state.current = 'chat:one'; renderMessages(rows)`);
  const play = () => find(f.get('transcript'), n => n.className?.startsWith('play-reply'));
  assert.equal(play().className, 'play-reply');
  play().events.click({stopPropagation() {}});
  const player = players.at(-1);
  assert.equal(player.src, '/api/conversations/chat%3Aone/speech/2');
  assert.equal(player.played, player.src);
  assert.equal(play().className, 'play-reply playing');
  play().events.click({stopPropagation() {}});
  assert.equal(player.paused, true);
  assert.equal(play().className, 'play-reply');
});

test('tap to record, tap to review the transcript, and send for a spoken reply', async () => {
  const {players, recorders, stream, globals} = voiceBrowser();
  const timers = [];
  const f = fixture(undefined, {...globals, setTimeout: fn => { timers.push(fn); return 1; }}); await tick();
  f.run(`state.current = 'one'; globalThis.calls = [];
    api = async (path, options = {}) => { calls.push([path, options.body]);
      return path === '/audio/transcribe' ? {transcript: 'what is on today'} : {session_id: 'one'}; };
    loadConversations = async () => {}; loadMessages = async () => {};`);
  const mic = f.get('voice-button');
  await mic.events.click();
  assert.equal(f.get('composer').classList.contains('recording'), true);
  assert.equal(players.length ? players[0].src : '', '', 'nothing plays while the microphone opens (iOS cuts the recording short)');
  assert.equal(recorders[0].state, 'recording', 'releasing the button does not stop recording');
  mic.events.click();
  for (let i = 0; i < 5; i++) await tick();
  assert.equal(stream.stopped, true);
  assert.equal(f.get('composer').classList.contains('recording'), false);
  const calls = JSON.parse(JSON.stringify(f.run('calls')));
  assert.deepEqual(calls[0], ['/audio/transcribe', '{"data_url":"data:audio/mp4;base64,AAAA","mime_type":"audio/mp4"}']);
  assert.equal(calls.length, 1, 'dictation must not send a message');
  assert.equal(f.get('message').value, 'what is on today');
  const now = Date.now() / 1000;
  // The same words were said earlier; the reply read is the one to this message, found by position.
  f.context.rows = [{id: 1, role: 'user', content: 'what is on today', timestamp: now},
                    {id: 2, role: 'assistant', content: 'Nothing yet.', timestamp: now}];
  f.run('renderMessages(rows)');
  await f.run('submitMessage({preventDefault() {}})');
  assert.equal(players[0].src, '/api/audio/silence', 'sending the voice draft unlocks the player for the reply');
  assert.equal(f.run('calls[1][0]'), '/conversations/one/messages');
  assert.equal(f.run('state.voiceReply.key'), f.run('state.sentMessages[0].key'));
  f.context.rows.push({id: 3, role: 'user', content: 'what is on today', timestamp: now});
  f.run(`state.turn = {id: 'one', working: true, at: Date.now()}; renderMessages(rows)`);
  assert.equal(f.run('state.voiceReply.row'), 3);
  assert.equal(timers.length, 0, 'nothing is read while the agent is working');
  f.context.rows.push({id: 4, role: 'assistant', content: 'Two meetings.', timestamp: now});
  f.run(`state.turn = {id: 'one', working: false, at: Date.now()}; renderMessages(rows)`);
  timers.forEach(fn => fn());
  assert.equal(players[0].src, '/api/conversations/one/speech/4');
  assert.equal(f.run('state.voiceReply'), null);
});


test('cancelling pending microphone permission releases a late stream without recording', async () => {
  const {recorders, stream, globals} = voiceBrowser();
  let allow;
  globals.navigator.mediaDevices.getUserMedia = () => new Promise(resolve => { allow = resolve; });
  const f = fixture(undefined, globals); await tick();
  const starting = f.get('voice-button').events.click();
  assert.equal(f.get('voice-status').classList.contains('hidden'), false);
  f.get('voice-cancel').events.click();
  allow(stream); await starting;
  assert.equal(stream.stopped, true);
  assert.equal(recorders.length, 0);
  assert.equal(f.get('voice-status').classList.contains('hidden'), true);
});

test('recorder construction and startup failures always release the microphone', async () => {
  for (const stage of ['constructor', 'start']) {
    const {stream, globals} = voiceBrowser();
    globals.MediaRecorder = class {
      constructor() { if (stage === 'constructor') throw new Error('unsupported'); }
      start() { throw new Error('unavailable'); }
      addEventListener() {}
    };
    const f = fixture(undefined, globals); await tick();
    await f.get('voice-button').events.click();
    assert.equal(stream.stopped, true, stage);
    assert.equal(f.get('composer').classList.contains('recording'), false);
    assert.equal(f.get('send').disabled, false);
    assert.match(f.get('notice').textContent, /couldn’t start recording/);
  }
});

test('transcription appends to edits made while waiting and does not submit on Enter', async () => {
  const {globals} = voiceBrowser();
  const f = fixture(undefined, globals); await tick();
  f.run(`state.current = 'one'; api = () => new Promise(resolve => { globalThis.transcribed = resolve; });`);
  await f.get('voice-button').events.click();
  f.get('voice-button').events.click(); await tick();
  f.get('message').value = 'A draft typed while waiting';
  f.get('message').maxLength = 16000;
  await f.run('submitMessage({preventDefault() {}})');
  assert.equal(f.get('message').value, 'A draft typed while waiting');
  f.run(`transcribed({transcript:'The dictated part'});`); await tick();
  assert.equal(f.get('message').value, 'A draft typed while waiting\nThe dictated part');
  assert.equal(f.run('state.voiceDraft'), true);
  f.get('message').value = ''; f.get('message').events.input();
  assert.equal(f.run('state.voiceDraft'), false, 'clearing the voice draft restores typed reply behavior');
});

test('cancelled, navigated and signed-out transcriptions cannot change another draft', async () => {
  for (const action of ['stopRecording(true)', 'newConversation()', 'signedOut()']) {
    const {globals} = voiceBrowser();
    const f = fixture(undefined, globals); await tick();
    f.run(`state.current = 'one'; api = () => new Promise(resolve => { globalThis.transcribed = resolve; });`);
    await f.get('voice-button').events.click(); f.get('voice-button').events.click(); await tick();
    f.run(action); f.get('message').value = 'Keep this draft';
    f.run(`transcribed({transcript:'Old conversation speech'});`); await tick();
    assert.equal(f.get('message').value, 'Keep this draft', action);
    assert.equal(f.get('voice-status').classList.contains('hidden'), true, action);
  }
});

test('leaving the conversation stops an active microphone immediately', async () => {
  for (const action of ['newConversation()', 'signedOut()', 'setSection("jobs")', '$("back-to-list").events.click()']) {
    const {stream, globals} = voiceBrowser();
    const f = fixture(undefined, globals); await tick();
    f.run(`state.current = 'one'; globalThis.calls = []; api = async path => { calls.push(path); return []; };`);
    await f.get('voice-button').events.click();
    f.run(action); await tick();
    assert.equal(stream.stopped, true, action);
    assert.equal(f.run('calls.includes("/audio/transcribe")'), false, action);
    assert.equal(f.get('composer').classList.contains('recording'), false, action);
  }
});

test('empty, failed and oversized transcriptions preserve the draft and restore controls', async () => {
  for (const response of ['({transcript:""})', 'Promise.reject(new Error("Speech service unavailable"))', '({transcript:"too long"})']) {
    const {globals} = voiceBrowser();
    const f = fixture(undefined, globals); await tick();
    f.run(`api = async () => ${response};`);
    f.get('message').value = 'Existing draft'; f.get('message').maxLength = 14;
    await f.get('voice-button').events.click(); f.get('voice-button').events.click();
    await tick(); await tick();
    assert.equal(f.get('message').value, 'Existing draft', response);
    assert.equal(f.get('send').disabled, false, response);
    assert.equal(f.get('voice-status').classList.contains('hidden'), true, response);
    assert.notEqual(f.get('notice').textContent, '', response);
  }
});

test('a rejected voice-draft send retains spoken reply behavior when retried', async () => {
  const f = fixture(); await tick();
  f.get('message').value = 'Dictated draft';
  f.run(`state.current = 'one'; state.voiceDraft = true; api = async () => { throw new Error('offline'); }; loadMessages = async () => {};`);
  await f.run('submitMessage({preventDefault() {}})');
  assert.equal(f.get('message').value, '');
  assert.equal(f.run('state.sentMessages[0].voice'), true);
  assert.equal(f.run('state.sentMessages[0].failed'), true);
});


test('backgrounding the page releases the microphone without sending audio', async () => {
  const {stream, globals} = voiceBrowser();
  const f = fixture(undefined, globals); await tick();
  await f.get('voice-button').events.click();
  f.run('document.hidden = true; document.events.visibilitychange()');
  assert.equal(stream.stopped, true);
  assert.equal(f.get('composer').classList.contains('recording'), false);
  assert.equal(f.get('voice-status').classList.contains('hidden'), true);
});

test('late final chunks from a cancelled recording cannot disrupt the next recording', async () => {
  const {recorders, globals} = voiceBrowser();
  const streams = [];
  globals.navigator.mediaDevices.getUserMedia = async () => {
    const stream = {getTracks: () => [{stop() { stream.stopped = true; }}]};
    streams.push(stream); return stream;
  };
  const f = fixture(undefined, globals); await tick();
  await f.get('voice-button').events.click();
  recorders[0].stop = () => { recorders[0].state = 'inactive'; };
  f.get('voice-button').events.click();
  assert.equal(streams[0].stopped, true, 'stop releases the mic before the asynchronous final chunk');
  f.get('voice-cancel').events.click();
  await f.get('voice-button').events.click();
  recorders[0].events.dataavailable({data:{size:3,type:'audio/mp4'}});
  recorders[0].events.stop();
  assert.equal(recorders[1].state, 'recording');
  assert.equal(streams[1].stopped, undefined);
  assert.equal(f.get('composer').classList.contains('recording'), true);
  f.get('voice-cancel').events.click();
});


test('approval card shows literal commands and resolves only one request', async () => {
  const f = fixture(); await tick();
  f.run(`state.current = 'chat:one'; setApprovals([{request_id:'request-one', command:'<script>unsafe()</script>', description:'Write a note', choices:['once','deny']}]);`);
  assert.equal(f.get('approvals').classList.contains('hidden'), false);
  const card = f.get('approvals').children[0];
  assert.equal(card.children[2].tagName, 'PRE');
  assert.equal(card.children[2].textContent, '<script>unsafe()</script>');
  assert.equal(card.children[3].children[0].textContent, 'Allow once');
  f.run(`globalThis.decisions = []; api = async (url, options) => { decisions.push({url, options}); return {resolved:true}; }; loadApprovals = () => {};`);
  await f.run(`decideApproval('request-one', 'once')`);
  assert.equal(f.run('decisions[0].url'), '/conversations/chat%3Aone/approvals');
  assert.equal(f.run('decisions[0].options.body'), '{"request_id":"request-one","choice":"once"}');
  assert.match(f.get('approvals').children[0].textContent, /Allowed once/);
  f.run('signedOut()');
  assert.equal(f.get('approvals').classList.contains('hidden'), true);
});

test('expired, offline and duplicate approval clicks never masquerade as success', async () => {
  const f = fixture(); await tick();
  f.run(`state.current = 'one'; setApprovals([{request_id:'r', command:'example', choices:['once','deny']}]);
    globalThis.calls = 0; api = () => { calls++; return new Promise((_, reject) => { globalThis.rejectDecision = reject; }); }; loadApprovals = () => {};`);
  const pending = f.run(`decideApproval('r', 'once')`);
  await f.run(`decideApproval('r', 'deny')`);
  assert.equal(f.run('calls'), 1);
  f.run(`rejectDecision(new Error('This request expired.'))`); await pending;
  assert.equal(f.run('state.approvalReady'), false);
  assert.match(f.run('state.approvalNotice'), /expired/);
  assert.equal(f.get('approvals').children[0].children[2].children[0].disabled, true);
});

test('pending approvals recover after reopening and stale polls cannot overwrite stream updates', async () => {
  const f = fixture(); await tick();
  f.run(`state.current = 'one'; globalThis.completePoll = null; api = () => new Promise(resolve => { completePoll = resolve; });`);
  const pending = f.run('loadApprovals()');
  f.run(`setApprovals([{request_id:'new', choices:['deny']}]); completePoll({approvals:[]});`); await pending;
  assert.equal(f.run('state.approvals[0].request_id'), 'new');
  f.run(`api = async () => ({approvals:[{request_id:'pending', description:'Recovered request', choices:['once','deny']}]});`);
  await f.run('loadApprovals()');
  assert.equal(f.run('state.approvals[0].request_id'), 'pending');
  f.run(`leaveLive(); state.current = 'two'; state.approvalFocus='old'; state.approvalFocusConversation='two'; setApprovals([]);`);
  assert.match(f.get('approvals').children[0].textContent, /no longer waiting/);
});

function workerFixture(maxActions = 2, fetch = async () => ({ok:true})) {
  const events = {}, notifications = [], opened = [], requests = [];
  const self = {location:{origin:'https://roost.example.invalid'}, Notification:{maxActions},
    addEventListener:(name, callback) => {events[name] = callback;},
    registration:{showNotification:async (title, options) => {notifications.push({title, options});}},
    clients:{matchAll:async () => [], openWindow:async url => {opened.push(url);}}};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../roost/static/sw.js'), 'utf8'),
    {self, URL, fetch:async (...args) => {requests.push(args); return fetch(...args);}});
  async function dispatch(name, event) { let done; events[name]({...event, waitUntil(value) {done=value;}}); await done; }
  return {notifications, requests, opened, dispatch};
}

test('notification actions use the owner session and exact request; Safari opens the card', async () => {
  for (const maxActions of [0,2]) {
    const f = workerFixture(maxActions);
    const payload = {title:'Kestrel needs approval', body:'Write a note', url:'/?c=one&a=request-one',
      approval:{conversation:'one', request_id:'request-one', choices:['once','deny']}};
    await f.dispatch('push', {data:{json:() => payload}});
    const notice = f.notifications[0];
    assert.equal(notice.options.actions.length, maxActions);
    await f.dispatch('notificationclick', {action:maxActions ? 'once' : '', notification:{...notice.options, close() {}}});
    if (maxActions) {
      assert.equal(f.requests[0][0], '/api/conversations/one/approvals');
      assert.equal(f.requests[0][1].credentials, 'same-origin');
      assert.equal(f.requests[0][1].headers['X-Roost-Request'], '1');
      assert.equal(f.requests[0][1].body, '{"request_id":"request-one","choice":"once"}');
    } else {
      assert.equal(f.requests.length, 0);
      assert.equal(f.opened[0], 'https://roost.example.invalid/?c=one&a=request-one');
    }
  }
});

test('failed notification decisions open Roost without replaying or implying approval', async () => {
  const f = workerFixture(2, async () => ({ok:false, status:401}));
  await f.dispatch('notificationclick', {action:'deny', notification:{tag:'r', close() {}, data:{url:'/?c=one&a=r', approval:{conversation:'one', request_id:'r'}}}});
  assert.equal(f.requests.length, 1);
  assert.match(f.notifications[0].options.body, /not confirmed/);
  assert.equal(f.opened.length, 1);
});

test('polling and live updates preserve a transcript selection until it is cleared', async () => {
  const f = fixture(); await tick();
  f.run(`state.current = 'one'; globalThis.rows = [{role:'assistant', content:'Copy this reply.'}]; renderMessages(rows);
    globalThis.selected = true;
    window.getSelection = () => ({isCollapsed: !selected, rangeCount:1, getRangeAt:() => ({intersectsNode:node => node === $('transcript')})});`);
  const nodes = f.get('transcript').children;
  f.run(`renderMessages([{role:'assistant', content:'Copy this reply. More text.'}])`);
  assert.equal(f.get('transcript').children, nodes, 'polling must not replace selected nodes');
  f.run(`selected = false; document.events.selectionchange()`);
  assert.match(text(f.get('transcript')), /More text/);
  f.run(`state.live = {id:'one', user:'Question', started:1, thoughts:'', text:'Streaming', done:false}; renderMessages(rows); selected = true;`);
  const liveBody = f.run('state.liveNodes.body');
  f.run(`state.live.text += ' continues'; renderLive()`);
  assert.equal(text(liveBody), 'Streaming');
  f.run(`selected = false; document.events.selectionchange()`);
  assert.match(text(f.get('transcript')), /Streaming continues/);
});

test('a second submitted message stays visible while the first reply is streaming', async () => {
  const f = fixture(); await tick();
  const now = Date.now() / 1000;
  f.context.rows = [{id:1,role:'user',content:'First question',timestamp:now},
    {id:2,role:'assistant',content:'Working on it',tool_calls:[{id:'tool-one'}],timestamp:now},
    {id:3,role:'user',content:'Second question',timestamp:now}];
  f.run(`state.current='one'; state.live={id:'one',user:'First question',started:1,thoughts:'Checking',text:'Working on it',done:false,after:0};
    renderMessages(rows);`);
  assert.match(text(f.get('transcript')), /First question/);
  assert.match(text(f.get('transcript')), /Second question/, 'the accepted follow-up must be visible during the first turn');
});


test('accepted follow-ups appear before Hermes persists them, including repeated text', async () => {
  const f = fixture(); await tick();
  f.context.rows = [{id:1,role:'user',content:'First question',timestamp:Date.now()/1000}];
  f.run(`state.current='one'; renderMessages(rows); api=async()=>({session_id:'one'});
    loadConversations=async()=>{}; loadMessages=async()=>renderMessages(rows);`);
  for (let i=0;i<2;i++) {
    f.get('message').value='Follow up';
    await f.run('submitMessage({preventDefault(){}})');
  }
  const own = () => f.get('transcript').children.filter(n=>n.className==='message from-you').map(text);
  assert.deepEqual(own(), ['First question','Follow up','Follow up']);
  f.context.rows.push({id:2,role:'user',content:'Follow up',timestamp:Date.now()/1000});
  f.run('renderMessages(rows)');
  assert.deepEqual(own(), ['First question','Follow up','Follow up'], 'one persisted copy must consume only one local echo');
  f.run('renderMessages(rows)');
  assert.deepEqual(own(), ['First question','Follow up','Follow up'], 'a later poll must not consume the second echo with the same row');
  f.context.rows.push({id:3,role:'user',content:'Follow up',timestamp:Date.now()/1000});
  f.run('renderMessages(rows)');
  assert.deepEqual(own(), ['First question','Follow up','Follow up']);
  f.run('signedOut()');
  assert.doesNotMatch(text(f.get('transcript')), /Follow up/);
});


test('a follow-up send retires the old stream instead of assigning it to the new question', async () => {
  const {opened,globals}=eventSources();
  const f=fixture(undefined,globals);await tick();
  f.run(`state.current='one'; state.lastRows=[{id:1,role:'user',content:'First',timestamp:Date.now()/1000}];
    api=async()=>({session_id:'one'}); loadConversations=async()=>{}; loadMessages=async()=>renderMessages(state.lastRows); openStream('one');`);
  opened[0].send({reset:true,user:'First',started:1,text:'First response'});
  f.get('message').value='Second';
  await f.run('submitMessage({preventDefault(){}})');
  assert.equal(opened[0].closed,true);
  assert.equal(f.run('state.live'),null);
  assert.match(text(f.get('transcript')),/Second/);
});

test('a completed first stream does not hide later replies or wait for their turn to finish', async () => {
  const f=fixture();await tick();
  f.context.rows=[{id:1,role:'user',content:'First',timestamp:Date.now()/1000},
    {id:2,role:'assistant',content:'First final',timestamp:Date.now()/1000},
    {id:3,role:'user',content:'Second',timestamp:Date.now()/1000}];
  f.run(`state.current='one';state.live={id:'one',user:'First',started:1,thoughts:'',text:'First stream',done:true,after:0};renderMessages(rows);`);
  assert.equal(f.run('state.live'),null);
  assert.match(text(f.get('transcript')),/First final/);
  assert.match(text(f.get('transcript')),/Second/);
});

test('Hermes may persist a queued burst as one combined user row without leaving duplicate local echoes', async () => {
  const f=fixture();await tick();
  f.context.rows=[{id:1,role:'user',content:'First',timestamp:Date.now()/1000}];
  f.run(`state.current='one';renderMessages(rows);api=async()=>({session_id:'one'});loadConversations=async()=>{};loadMessages=async()=>renderMessages(rows);`);
  for(const line of ['Same follow up','Same follow up']) {
    f.get('message').value=line;await f.run('submitMessage({preventDefault(){}})');
  }
  f.context.rows.push({id:2,role:'assistant',content:'First reply',timestamp:Date.now()/1000},
    {id:3,role:'user',content:'Same follow up\n\nSame follow up',timestamp:Date.now()/1000});
  f.run('renderMessages(rows)');
  assert.equal(f.run('state.sentMessages.length'),0,'the combined durable row covers both accepted sends');
  assert.equal(f.get('transcript').children.filter(n=>n.className==='message from-you').length,2);
});

test('thinking level: a new conversation sends it with the first message, an open one saves it at once', async () => {
  const f = fixture(); await tick();
  f.run(`globalThis.calls = []; api = async (path, options = {}) => { calls.push([path, options.method || 'GET', options.body]);
    return path.endsWith('/settings') ? {instructions: '', reasoning: JSON.parse(options.body || '{"reasoning":"high"}').reasoning} : {session_id: 'one'}; };
    loadConversations = async () => {}; loadMessages = async () => {}; newConversation();`);
  f.get('reasoning').value = 'none'; await f.run(`changeReasoning()`);
  assert.equal(f.run('calls.length'), 0, 'nothing to save before the conversation exists');
  assert.ok(f.get('reasoning-chip').classList.contains('chosen'));
  f.get('message').value = 'hello'; await f.run(`submitMessage({preventDefault() {}})`);
  assert.equal(JSON.parse(f.run('calls[0][2]')).text, 'hello');
  assert.equal(JSON.parse(f.run('calls[0][2]')).reasoning, 'none');
  f.get('reasoning').value = 'low'; await f.run(`changeReasoning()`);
  assert.deepEqual(JSON.parse(f.run('JSON.stringify(calls.at(-1).slice(0, 2))')), ['/conversations/one/settings', 'PUT']);
  f.run(`newConversation()`);
  assert.equal(f.get('reasoning').value, 'default');
  assert.ok(!f.get('reasoning-chip').classList.contains('chosen'));
});

test('a failed thinking change puts the previous level back', async () => {
  const f = fixture(); await tick();
  f.run(`state.current = 'one'; showReasoning('high'); api = async () => { throw new Error('offline'); };`);
  f.get('reasoning').value = 'none'; await f.run(`changeReasoning()`);
  assert.equal(f.get('reasoning').value, 'high');
  assert.equal(f.run('state.reasoning'), 'high');
});

test('a new conversation focuses the message field only without a touch screen', async () => {
  for (const [coarse, expected] of [[false, 1], [true, 0]]) {
    const window = { roostTheme: { current: () => 'dark' }, addEventListener() {}, getSelection: () => '',
      matchMedia: query => ({matches: coarse && query === '(pointer: coarse)'}) };
    const f = fixture(undefined, {window}); await tick();
    let focused = 0; f.get('message').focus = () => focused++;
    f.run(`loadConversations = async () => {}; newConversation();`);
    assert.equal(focused, expected, coarse ? 'touch screen' : 'mouse');
  }
});

test('notification messages wait for sign-in, open once, and clear on sign-out', async () => {
  let message;
  const f = fixture(undefined, {navigator: {serviceWorker: {addEventListener(type, fn) { message = fn; }}}});
  await tick();
  f.run(`globalThis.opened = []; openLink = link => opened.push(link);
    refreshIdentity = () => {}; loadConversations = () => new Promise(resolve => {globalThis.finishList = resolve;});`);
  message({data: {link: '?c=one&a=approval'}});
  const signingIn = f.run('signedIn()');
  message({data: {link: '?c=two&a=approval'}});
  assert.equal(f.run('opened.length'), 0);
  f.run('finishList()'); await signingIn;
  assert.deepEqual(Array.from(f.run('opened')), ['?c=two&a=approval']);
  f.run('loadConversations = async () => {}'); await f.run('signedIn()');
  assert.equal(f.run('opened.length'), 1);
  f.run('signedOut()'); message({data: {open: 'old'}}); f.run('signedOut()');
  await f.run('signedIn()'); assert.equal(f.run('opened.length'), 1);
});

test('cold notification URLs survive initial sign-out through password and passkey sign-in', async () => {
  for (const method of ['password', 'passkey']) for (const link of ['?c=one&a=approval', '?j=job', '?d=delivery']) {
    const f = passkeyFixture(); await tick();
    f.context.location = {search: link};
    f.context.history = {replaceState() { f.context.location.search = ''; }};
    f.run(`signedOut(false); globalThis.opened = []; openLink = link => opened.push(link);
      refreshIdentity = () => {}; loadConversations = async () => {};
      api = async path => path === '/passkeys/login/options' ? {challenge:'AQID', rpId:'roost.example.invalid'} : {};`);
    if (method === 'password') await f.get('login-form').events.submit({preventDefault() {}});
    else await f.run('passkeySignIn()');
    assert.deepEqual(Array.from(f.run('opened')), [link]);
    await f.run('signedIn()'); assert.equal(f.run('opened.length'), 1);
  }
});

test('resume refreshes the list and open view and shows reconnecting until success', async () => {
  const f = fixture(); await tick();
  f.run(`show('app', true); state.current = 'one'; globalThis.loads = [];
    loadConversations = async () => {loads.push('list');}; loadMessages = async () => {loads.push('chat');};
    loadJobs = async () => {loads.push('job');};`);
  f.context.document.visibilityState = 'visible';
  f.context.document.events.visibilitychange(); await tick();
  assert.deepEqual(Array.from(f.run('loads')), ['list', 'chat']);
  f.run(`state.section = 'jobs'; loadJobs = async () => {throw new Error('offline');};`);
  f.context.document.events.visibilitychange(); await tick();
  assert.equal(f.get('presence').textContent, 'Reconnecting…');
  f.run(`loadJobs = async () => {loads.push('job');};`);
  f.context.document.events.visibilitychange(); await tick();
  assert.equal(f.get('presence').textContent, 'Online');
  assert.equal(f.run('loads.at(-1)'), 'job');
  f.run('signedOut(); loads = []'); f.context.document.events.visibilitychange(); await tick();
  assert.equal(f.run('loads.length'), 0);
});

test('background poll failures show reconnecting and a successful poll clears it', async () => {
  const polls = new Map();
  const f = fixture(undefined, {setInterval(fn, ms) { polls.set(ms, fn); }}); await tick();
  f.run(`show('app', true); state.current = 'one'; loadMessages = async () => {throw new Error('offline');};`);
  polls.get(3000)(); await tick(); assert.equal(f.get('presence').textContent, 'Reconnecting…');
  f.run('loadMessages = async () => {}'); polls.get(3000)(); await tick();
  assert.equal(f.get('presence').textContent, 'Online');
  f.run(`loadConversations = async () => {throw new Error('offline');};`);
  polls.get(20000)(); await tick(); assert.equal(f.get('presence').textContent, 'Reconnecting…');
});

test('reply and code buttons copy literal text with local success and failure feedback', async () => {
  const copied = [], timers = [];
  const f = fixture(undefined, {navigator: {clipboard: {writeText: async value => copied.push(value)}},
    setTimeout(fn) { timers.push(fn); return timers.length; }}); await tick();
  f.context.reply = '**Hello**\n\n```js\nconst x = 1;\n```';
  f.run(`state.current = 'one'; renderMessages([{id:1, role:'assistant', content:reply}]);`);
  const find = (n, label) => n.title === label ? n : (n.children || []).map(c => find(c, label)).find(Boolean);
  const reply = find(f.get('transcript'), 'Copy reply'), code = find(f.get('transcript'), 'Copy code');
  assert.ok(reply); assert.ok(code);
  const event = {stopPropagation() {}};
  await reply.events.click(event); await code.events.click(event);
  assert.deepEqual(copied, [f.context.reply, 'const x = 1;']);
  assert.equal(reply.textContent, 'Copied'); assert.equal(code.textContent, 'Copied');
  timers.splice(0).forEach(fn => fn()); assert.equal(reply.title, 'Copy reply');
  f.context.navigator.clipboard.writeText = async () => {throw new Error('denied');};
  await code.events.click(event); assert.equal(code.textContent, 'Copy failed');
});

test('jump to latest preserves scroll, marks changed replies and clears at the bottom', async () => {
  const f = fixture(); await tick();
  f.run(`state.current = 'one'; globalThis.rows = [{id:1,role:'assistant',content:'First'}]; renderMessages(rows);`);
  const transcript = f.get('transcript'), jump = f.get('jump-latest');
  transcript.scrollHeight = 1000; transcript.clientHeight = 200; transcript.scrollTop = 100;
  transcript.events.scroll(); assert.equal(jump.classList.contains('hidden'), false);
  f.run('renderMessages(rows)'); assert.equal(jump.classList.contains('unread'), false);
  f.run(`rows.push({id:2,role:'assistant',content:'New reply'}); renderMessages(rows);`);
  assert.equal(transcript.scrollTop, 100); assert.equal(jump.classList.contains('unread'), true);
  jump.events.click(); assert.equal(transcript.scrollTop, 1000);
  assert.equal(jump.classList.contains('hidden'), true); assert.equal(jump.classList.contains('unread'), false);
  transcript.scrollTop = 750; transcript.events.scroll(); assert.equal(jump.classList.contains('hidden'), true);
  f.run('signedOut()'); assert.equal(jump.classList.contains('hidden'), true);
});

test('job list poll failures reach the reconnecting status', async () => {
  const f = fixture(); await tick();
  f.run(`show('app', true); state.section = 'jobs'; state.selectedJob = 'one';
    api = async () => { throw new Error('offline'); };
    renderJobs = () => {}; renderJobDetail = () => {};`);
  await f.run('refreshConnection(() => loadJobs())');
  assert.equal(f.get('presence').textContent, 'Reconnecting…');
  assert.ok(f.get('jobs-header').children.includes(f.get('presence')));
  f.run(`api = async path => path === '/jobs' ? {jobs:[{id:'one'}]} : {runs:[]};`);
  await f.run('refreshConnection(() => loadJobs())');
  assert.equal(f.get('presence').textContent, 'Online');
});

test('phone reconnecting status follows the visible list and detail headers', async () => {
  const f = fixture(); await tick();
  f.run(`phoneWidth.matches = true; show('app', true); setConnection(false);`);
  assert.ok(f.get('mobile-list-actions').children.includes(f.get('presence')));
  f.run(`loadMessages = async () => {}; loadReasoning = () => {}; selectConversation('one');`);
  assert.ok(f.get('chat-actions').children.includes(f.get('presence')));
  f.get('back-to-list').events.click();
  assert.equal(f.get('mobile-list-actions').children.at(-1), f.get('presence'));
  f.run(`state.section = 'jobs'; renderJobs = () => {}; state.jobRuns.one = []; selectJob('one');`);
  assert.equal(f.get('jobs-header').children.at(-1), f.get('presence'));
  f.get('back-to-jobs').events.click();
  assert.equal(f.get('mobile-list-actions').children.at(-1), f.get('presence'));
});

test('a notification received during boot survives the initial signed-out response', async () => {
  let finish, message;
  const f = fixture(() => new Promise(resolve => {finish = resolve;}),
    {navigator: {serviceWorker: {addEventListener(type, fn) {message = fn;}}}});
  message({data: {link: '?d=delivery'}});
  finish({ok:true, json:async () => ({signed_in:false})}); await tick();
  f.run(`globalThis.opened = ''; openLink = link => {opened = link;}; refreshIdentity = () => {}; loadConversations = async () => {};`);
  await f.run('signedIn()'); assert.equal(f.run('opened'), '?d=delivery');
});

test('live reply chunks mark jump to latest without moving the reader', async () => {
  const f = fixture(); await tick();
  f.run(`state.backend = 'native'; state.current = 'one'; state.lastRows = [];
    state.live = {id:'one', text:'First chunk', thoughts:'', trace:[], sources:[]}; liveNodes(state.live);`);
  const transcript = f.get('transcript'), jump = f.get('jump-latest');
  transcript.scrollHeight = 1000; transcript.clientHeight = 200; transcript.scrollTop = 100;
  f.run('renderLive()'); assert.equal(jump.classList.contains('unread'), false);
  f.run(`state.live.text += ' next chunk'; renderLive();`);
  assert.equal(transcript.scrollTop, 100); assert.equal(jump.classList.contains('unread'), true);
  jump.classList.remove('unread');
  f.run('renderLive()'); assert.equal(jump.classList.contains('unread'), false);
  f.run(`state.live.sources = [{n:1, title:'Example', url:'https://example.org'}]; renderLive();`);
  assert.equal(jump.classList.contains('unread'), true);
});

test('failed messages stay in their conversation and Retry sends once without consuming a new draft', async () => {
  const f = fixture(); await tick();
  f.run(`state.current = 'one'; globalThis.posts = []; api = async (path, options) => {
    if (options?.method === 'POST') { posts.push(JSON.parse(options.body).text); throw new Error('rejected'); }
    return {messages:[]}; }; loadReasoning = () => {};`);
  f.get('message').value = 'Keep this prompt';
  await f.run('submitMessage({preventDefault() {}})');
  assert.match(text(f.get('transcript')), /Keep this prompt.*Not sent.*Retry.*Delete/s);
  assert.equal(f.run('state.working'), false);
  await f.run(`selectConversation('two')`);
  assert.doesNotMatch(text(f.get('transcript')), /Keep this prompt/);
  await f.run(`selectConversation('one')`);
  f.get('message').value = 'Next draft'; f.get('message').events.input();
  f.run(`api = (path, options) => {
    if (options?.method === 'POST') { posts.push(JSON.parse(options.body).text); return new Promise(resolve => {globalThis.finish = resolve;}); }
    return Promise.resolve({messages:[]}); }; loadConversations = async () => {}; loadMessages = async () => {};`);
  const retry = f.get('transcript').children.flatMap(n => n.children).find(n => n.textContent === 'Retry');
  const first = retry.events.click(); const second = retry.events.click();
  assert.equal(f.run('posts.length'), 2);
  f.run(`finish({session_id:'one'})`); await Promise.all([first, second]);
  assert.equal(f.get('message').value, 'Next draft');
  assert.equal(f.run('state.sentMessages.filter(row => row.failed).length'), 0);
});

test('a timed-out send keeps its key on Retry and uses the cached acknowledgment for queued and coalesced text', async () => {
  for (const saved of [[], [{id:2, role:'user', content:'Accepted prompt\n\nOther prompt'}]]) {
    const f = fixture(); await tick();
    const posts = [], entries = new Map(); let hermes = 0;
    f.context.fetch = async (url, options = {}) => {
      if (options.method !== 'POST') return {ok:true, json:async () => ({messages:saved})};
      const body = JSON.parse(options.body); posts.push(body);
      if (entries.has(body.request_id)) return {ok:true, json:async () => ({session_id:entries.get(body.request_id)})};
      hermes++; entries.set(body.request_id, 'one');
      const error = new Error('timed out after acceptance'); error.name = 'TimeoutError'; throw error;
    };
    f.run(`state.current = 'one'; loadConversations = async () => {}; loadMessages = async () => {};`);
    f.get('message').value = 'Accepted prompt'; await f.run('submitMessage({preventDefault() {}})');
    assert.equal(f.run('state.sentMessages[0].pending'), true);
    assert.doesNotMatch(text(f.get('transcript')), /Not sent/);
    await f.run('retryMessage(state.sentMessages[0])');
    assert.equal(posts.length, 2); assert.equal(hermes, 1);
    assert.equal(posts[0].request_id, posts[1].request_id);
    assert.match(posts[0].request_id, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
    assert.equal(f.run('state.sentMessages[0].failed'), false);
  }
});

test('an accepted send releases busy even while its refresh never settles', async () => {
  const f = fixture(); await tick();
  f.run(`state.current = 'one'; api = async () => ({session_id:'one'});
    loadConversations = () => new Promise(() => {}); loadMessages = () => new Promise(() => {});`);
  f.get('message').value = 'Hello';
  f.run('submitMessage({preventDefault() {}})'); await tick();
  assert.equal(f.run('state.busy'), false);
  assert.equal(f.get('send').disabled, false);
});

test('composer drafts restore per conversation, survive reload and clear on accepted send and sign-out', async () => {
  const stored = new Map(), localStorage = {getItem: key => stored.get(key) || null,
    setItem: (key, value) => stored.set(key, value), removeItem: key => stored.delete(key)};
  const f = fixture(undefined, {localStorage}); await tick();
  f.run(`loadMessages = async () => {}; loadReasoning = () => {}; loadConversations = async () => {}; newConversation();`);
  f.get('message').value = 'New draft'; f.get('message').events.input();
  await f.run(`selectConversation('one')`); assert.equal(f.get('message').value, '');
  f.get('message').value = 'One draft'; f.get('message').events.input();
  await f.run(`selectConversation('two')`);
  f.get('message').value = 'Two draft'; f.get('message').events.input();
  await f.run(`selectConversation('one')`); assert.equal(f.get('message').value, 'One draft');
  f.run('newConversation()'); assert.equal(f.get('message').value, 'New draft');
  const reloaded = fixture(async () => ({ok:true, json:async () => ({signed_in:true})}), {localStorage});
  reloaded.run(`signedIn = async () => {}; loadMessages = async () => {}; loadReasoning = () => {};`); await tick();
  await reloaded.run(`selectConversation('two')`); assert.equal(reloaded.get('message').value, 'Two draft');
  await f.run(`selectConversation('one'); api = async () => ({session_id:'one'}); submitMessage({preventDefault() {}})`);
  await f.run(`selectConversation('two'); selectConversation('one')`); assert.equal(f.get('message').value, '');
  f.run('signedOut()'); assert.equal(stored.size, 0);
});

test('GET and send timeouts preserve caller cancellation with and without AbortSignal.any', async () => {
  for (const any of [AbortSignal.any.bind(AbortSignal), undefined]) {
    const timeouts = [];
    const signals = {any, timeout(ms) { const controller = new AbortController(); timeouts.push({ms, controller}); return controller.signal; }};
    const f = fixture(undefined, {AbortSignal: signals, AbortController}); await tick(); timeouts.length = 0;
    for (const [path, options, ms] of [['/jobs', {}, 15000], ['/jobs', {method:'GET'}, 15000],
      ['/conversations', {method:'POST', body:'{}'}, 50000], ['/conversations/one/messages', {method:'POST', body:'{}'}, 50000]]) {
      let signal;
      f.context.fetch = async (url, options) => { signal = options.signal; return {ok:true, json:async () => ({})}; };
      f.context.options = options; f.context.path = path;
      await f.run('api(path, options)');
      assert.equal(timeouts.at(-1).ms, ms); assert.ok(signal);
    }
    for (const cancel of ['caller', 'timeout', 'already aborted']) {
      const caller = new AbortController(), error = new DOMException('cancelled', 'TimeoutError');
      if (cancel === 'already aborted') caller.abort(error);
      f.context.options = {signal: caller.signal};
      f.context.fetch = (url, {signal}) => new Promise((resolve, reject) => {
        if (signal.aborted) reject(signal.reason);
        else signal.addEventListener('abort', () => reject(signal.reason), {once:true});
      });
      const request = f.run(`api('/jobs', options)`);
      if (cancel === 'caller') caller.abort(error);
      if (cancel === 'timeout') timeouts.at(-1).controller.abort(error);
      await assert.rejects(request, /cancelled/);
    }
  }
});

test('a send timeout during response parsing stays ambiguous instead of accepting an empty response', async () => {
  const controllers = [];
  const f = fixture(undefined, {AbortSignal: {timeout() { const controller = new AbortController(); controllers.push(controller); return controller.signal; }, any:AbortSignal.any.bind(AbortSignal)}});
  await tick();
  f.context.fetch = async (url, {signal}) => ({ok:true, json:() => new Promise((resolve, reject) => {
    signal.addEventListener('abort', () => reject(signal.reason), {once:true});
  })});
  const sending = f.run(`api('/conversations/one/messages', {method:'POST', body:'{}'})`);
  await tick(); controllers.at(-1).abort(new DOMException('timed out', 'TimeoutError'));
  await assert.rejects(sending, /timed out/);
});

test('failed new conversations retain voice on Retry and can Delete a failed row', async () => {
  const f = fixture(); await tick();
  f.run(`newConversation(); state.voiceDraft = true; api = async () => {throw new Error('rejected');};`);
  f.get('message').value = 'Dictated new prompt';
  await f.run('submitMessage({preventDefault() {}})');
  await f.run(`loadMessages = async () => {}; loadReasoning = () => {}; selectConversation('other');`);
  f.run('newConversation()'); assert.match(text(f.get('transcript')), /Dictated new prompt.*Not sent/s);
  f.run(`globalThis.posts = []; api = async (path, options) => {posts.push([path, JSON.parse(options.body)]); return {session_id:'created'};};
    loadConversations = async () => {};`);
  const retry = f.get('transcript').children.flatMap(n => n.children).find(n => n.textContent === 'Retry');
  await retry.events.click();
  assert.equal(f.run('posts.length'), 1); assert.equal(f.run('posts[0][0]'), '/conversations');
  assert.equal(f.run('posts[0][1].text'), 'Dictated new prompt');
  assert.equal(f.run('state.voiceReply.id'), 'created'); assert.equal(f.run('state.current'), 'created');
  f.run(`api = async () => {throw new Error('rejected');}`); f.get('message').value = 'Discard me';
  await f.run('submitMessage({preventDefault() {}})');
  const remove = f.get('transcript').children.flatMap(n => n.children).find(n => n.textContent === 'Delete');
  remove.events.click(); assert.doesNotMatch(text(f.get('transcript')), /Discard me/);
  f.run('signedOut()'); assert.equal(f.run('state.sentMessages.length'), 0);
});

test('Retry of a new send receives its created id from the cached result', async () => {
  const f = fixture(); await tick();
  const posts = [], entries = new Map(); let hermes = 0;
  f.context.fetch = async (url, options = {}) => {
    assert.equal(url, '/api/conversations');
    const body = JSON.parse(options.body); posts.push(body);
    if (entries.has(body.request_id)) return {ok:true, json:async () => ({session_id:'created'})};
    hermes++; entries.set(body.request_id, 'created'); throw new TypeError('lost response');
  };
  f.run(`newConversation(); loadMessages = async () => {}; loadConversations = async () => {};`);
  f.get('message').value = 'New prompt'; await f.run('submitMessage({preventDefault() {}})');
  await f.run('retryMessage(state.sentMessages[0])');
  assert.equal(posts.length, 2); assert.equal(hermes, 1);
  assert.equal(posts[0].request_id, posts[1].request_id); assert.equal(f.run('state.current'), 'created');
});

test('a 503 after acceptance and repeated pending responses never make another Hermes call', async () => {
  const f = fixture(); await tick();
  const posts = [], entries = new Map(); let hermes = 0;
  f.context.fetch = async (url, options = {}) => {
    const body = JSON.parse(options.body); posts.push(body);
    if (entries.has(body.request_id)) return {ok:false, status:409, json:async () => ({pending:true, error:'Still sending. Check again shortly.'})};
    hermes++; entries.set(body.request_id, 'pending');
    return {ok:false, status:503, json:async () => ({error:'Acknowledgment lost'})};
  };
  f.run(`state.current = 'one'; loadMessages = async () => {}; loadConversations = async () => {};`);
  f.get('message').value = 'Late prompt'; await f.run('submitMessage({preventDefault() {}})');
  await f.run('retryMessage(state.sentMessages[0])'); await f.run('retryMessage(state.sentMessages[0])');
  assert.equal(posts.length, 3); assert.equal(hermes, 1);
  assert.equal(new Set(posts.map(row => row.request_id)).size, 1);
  assert.equal(f.run('state.sentMessages[0].pending'), true);
  assert.match(text(f.get('transcript')), /Sending….*Check again/s);
  assert.doesNotMatch(text(f.get('transcript')), /Not sent/);
  assert.equal(f.run('state.busy'), false);
});

test('deliberately repeated text gets distinct keys and an earlier row cannot confirm the second send', async () => {
  const f = fixture(); await tick();
  const posts = [], entries = new Map(); let hermes = 0;
  f.context.fetch = async (url, options = {}) => {
    const body = JSON.parse(options.body); posts.push(body);
    if (entries.has(body.request_id)) return {ok:false, status:409, json:async () => ({pending:true})};
    hermes++; entries.set(body.request_id, 'pending');
    return hermes === 1 ? {ok:true, json:async () => ({session_id:'one'})}
      : {ok:false, status:503, json:async () => ({error:'Reply lost'})};
  };
  f.run(`state.current = 'one'; loadMessages = async () => {}; loadConversations = async () => {};`);
  f.get('message').value = 'Again'; await f.run('submitMessage({preventDefault() {}})');
  f.run(`renderMessages([{id:1, role:'user', content:'Again', timestamp:Date.now()/1000}]);`);
  f.get('message').value = 'Again'; await f.run('submitMessage({preventDefault() {}})');
  await f.run('retryMessage(state.sentMessages[0])');
  assert.equal(posts.length, 3); assert.equal(hermes, 2);
  assert.notEqual(posts[0].request_id, posts[1].request_id);
  assert.equal(posts[1].request_id, posts[2].request_id);
  assert.equal(f.run('state.sentMessages[0].pending'), true);
});

test('drafts work without storage and retain only the 50 most recently edited plain texts', async () => {
  for (const broken of [false, true]) {
    const stored = new Map();
    const localStorage = {getItem: key => stored.get(key) || null, setItem(key, value) {
      if (broken) throw new Error('blocked'); stored.set(key, value);
    }, removeItem(key) { if (broken) throw new Error('blocked'); stored.delete(key); }};
    const f = fixture(undefined, {localStorage}); await tick();
    if (broken) Object.defineProperty(f.context, 'localStorage', {get() {throw new Error('blocked');}});
    f.run(`loadMessages = async () => {}; loadReasoning = () => {};`);
    for (let index = 0; index < 52; index++) {
      f.context.id = String(index); await f.run('selectConversation(id)');
      f.get('message').value = `draft ${index}`; f.get('message').events.input();
    }
    assert.equal(f.run('state.composerDrafts.length'), 50);
    await f.run(`selectConversation('2')`); assert.equal(f.get('message').value, 'draft 2');
    await f.run(`selectConversation('0')`); assert.equal(f.get('message').value, '');
    if (!broken) for (const row of JSON.parse([...stored.values()][0])) assert.deepEqual(Object.keys(row), ['id', 'text']);
    f.run('signedOut()');
  }
});

test('a send failing after navigation stays with its original conversation and cannot return after sign-out', async () => {
  for (const signOut of [false, true]) {
    const f = fixture(); await tick();
    f.run(`state.current = 'one'; api = (path, options) => options?.method === 'POST'
      ? new Promise((resolve, reject) => {globalThis.fail = reject;}) : Promise.resolve({messages:[]});
      loadReasoning = () => {};`);
    f.get('message').value = 'Original prompt'; const sending = f.run('submitMessage({preventDefault() {}})');
    if (signOut) f.run('signedOut()'); else await f.run(`selectConversation('two')`);
    f.get('message').value = 'Other draft'; f.get('message').events.input();
    f.run(`fail(new Error('rejected'))`); await sending;
    assert.equal(f.get('message').value, 'Other draft');
    assert.doesNotMatch(text(f.get('transcript')), /Original prompt/);
    if (signOut) assert.equal(f.run('state.sentMessages.length'), 0);
    else {
      await f.run(`selectConversation('one')`);
      assert.match(text(f.get('transcript')), /Original prompt.*Not sent/s);
    }
  }
});

test('persisting an accepted row never consumes or replaces an unrelated failed row', async () => {
  const f = fixture(); await tick();
  f.run(`state.current = 'one'; globalThis.failed = {id:'one', key:'failed', text:'Lost before', failed:true, since:1, after:0};
    state.sentMessages = [failed, {id:'one', key:'accepted', text:'Accepted', since:2, after:0}];
    renderMessages([{id:1, role:'user', content:'Accepted'}]);`);
  assert.equal(f.run('state.sentMessages[0] === failed'), true);
  assert.equal(f.run('state.sentMessages[0].after'), 0);
  assert.match(text(f.get('transcript')), /Lost before.*Not sent/s);
});


test('a new conversation migrates its follow-up draft even after navigation', async () => {
  const f = fixture(); await tick();
  f.run(`loadReasoning = () => {}; loadMessages = async () => {}; loadConversations = async () => {};
    api = (path, options) => options?.method === 'POST' ? new Promise(resolve => {globalThis.finish = resolve;}) : Promise.resolve({}); newConversation();`);
  f.get('message').value = 'First'; const sending = f.run('submitMessage({preventDefault() {}})');
  f.get('message').value = 'Follow-up'; f.get('message').events.input();
  await f.run(`selectConversation('other')`);
  f.get('message').value = 'Other draft'; f.get('message').events.input();
  f.run(`finish({session_id:'created'})`); await sending;
  assert.equal(f.get('message').value, 'Other draft');
  await f.run(`selectConversation('created')`); assert.equal(f.get('message').value, 'Follow-up');
  f.run('newConversation()'); assert.equal(f.get('message').value, '');
});

test('deleting the open conversation discards its draft and restores the new draft', async () => {
  const f = fixture(); await tick();
  f.run(`loadMessages = async () => {}; loadReasoning = () => {}; api = async () => ({}); newConversation();`);
  f.get('message').value = 'Unsaved new'; f.get('message').events.input();
  await f.run(`selectConversation('one')`);
  f.get('message').value = 'Deleted draft'; f.get('message').events.input();
  await f.run(`state.sheetItem = {id:'one'}; deleteConversation()`);
  assert.equal(f.get('message').value, 'Unsaved new');
  assert.equal(f.run(`composerDrafts().some(row => row.id === 'one')`), false);
  f.run('newConversation()'); assert.equal(f.get('message').value, 'Unsaved new');
});

test('initial signed-in welcome restores the stored new draft before any typing', async () => {
  const stored = new Map([['roost.composer-drafts', JSON.stringify([{id:'new', text:'Saved new'}])]]);
  const f = fixture(() => new Promise(() => {}), {localStorage: {getItem: key => stored.get(key), setItem: (key, value) => stored.set(key, value)}});
  f.run(`renderWelcome()`);
  assert.equal(f.get('message').value, 'Saved new');
  assert.equal(f.get('composer').classList.contains('has-text'), true);
  f.get('message').value += ' continued'; f.get('message').events.input();
  f.run('renderWelcome()'); assert.equal(f.get('message').value, 'Saved new continued');
});


test('send ids use getRandomValues when randomUUID is missing and Retry never generates another id', async () => {
  let generated = 0;
  const crypto = {getRandomValues(value) {generated++; return require('node:crypto').webcrypto.getRandomValues(value);}};
  const f = fixture(undefined, {crypto}); await tick();
  f.run(`state.current = 'one'; globalThis.posts = []; api = async (path, options) => {
    posts.push(JSON.parse(options.body)); throw new Error('rejected');
  };`);
  f.get('message').value = 'Retry safely'; await f.run('submitMessage({preventDefault() {}})');
  await f.run('retryMessage(state.sentMessages[0])');
  assert.equal(generated, 1); assert.equal(f.run('posts.length'), 2);
  assert.equal(f.run('posts[0].request_id'), f.run('posts[1].request_id'));
  assert.match(f.run('posts[0].request_id'), /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
});

function pendingClock() {
  let now = 1800000000000, next = 0;
  const timers = new Map();
  class ClockDate extends Date { static now() { return now; } }
  return {timers, globals: {Date: ClockDate,
    setTimeout(fn, ms = 0) { const id = ++next; timers.set(id, {fn, ms, at:now + ms}); return id; },
    clearTimeout(id) { timers.delete(id); }},
    async advance(ms) {
      const target = now + ms;
      for (;;) {
        const due = [...timers].filter(([, timer]) => timer.at <= target).sort((a, b) => a[1].at - b[1].at)[0];
        if (!due) break;
        now = due[1].at; timers.delete(due[0]); await due[1].fn(); await tick();
      }
      now = target;
    }, checks: () => [...timers.values()].filter(timer => timer.ms === 5000).length};
}

test('saved user rows retire pending echoes and their timers, including coalesced sends', async () => {
  const clock = pendingClock(), f = fixture(undefined, clock.globals); await tick();
  f.run(`state.current = 'one'; api = async () => {const error = new Error('Still sending'); error.pending = true; throw error;};`);
  for (const value of ['First', 'Second']) {
    f.get('message').value = value; await f.run('submitMessage({preventDefault() {}})');
  }
  assert.equal(clock.checks(), 2);
  f.run(`state.sentMessages.push({id:'one', key:'failed', text:'Not accepted', since:Date.now(), after:0, failed:true});
    renderMessages([{id:1, role:'user', content:'First\\n\\nSecond', timestamp:Date.now()/1000}]);`);
  assert.equal(f.run('state.sentMessages.length'), 1);
  assert.equal(f.run('state.sentMessages[0].key'), 'failed');
  assert.equal(clock.checks(), 0);
  const own = f.get('transcript').children.filter(n => n.className === 'message from-you');
  assert.equal(own.length, 2);
  assert.doesNotMatch(text(f.get('transcript')), /Sending…|Check again/);
  await clock.advance(15000); assert.equal(clock.checks(), 0);
});

test('pending auto-checks reuse one key and resolve with exactly one Hermes-bound submission', async () => {
  const clock = pendingClock(), f = fixture(undefined, clock.globals); await tick();
  const posts = [], entries = new Map(); let hermes = 0, done = false;
  f.context.fetch = async (url, options) => {
    const body = JSON.parse(options.body); posts.push(body);
    if (entries.has(body.request_id)) return done ? {ok:true, json:async () => ({session_id:'one'})}
      : {ok:false, status:409, json:async () => ({pending:true, error:'Still sending'})};
    hermes++; entries.set(body.request_id, 'pending');
    return {ok:false, status:503, json:async () => ({error:'Lost acknowledgment'})};
  };
  f.run(`state.current = 'one'; loadMessages = async () => {}; loadConversations = async () => {};`);
  f.get('message').value = 'Check automatically'; await f.run('submitMessage({preventDefault() {}})');
  assert.equal(clock.checks(), 1);
  await clock.advance(5000); assert.equal(posts.length, 2); assert.equal(clock.checks(), 1);
  done = true; await clock.advance(5000);
  assert.equal(posts.length, 3); assert.equal(hermes, 1);
  assert.equal(new Set(posts.map(row => row.request_id)).size, 1);
  assert.equal(f.run('state.sentMessages[0].pending'), false); assert.equal(clock.checks(), 0);
  await clock.advance(15000); assert.equal(posts.length, 3);
});

test('pending auto-checks stop two minutes after the first send, even after manual checks', async () => {
  const clock = pendingClock(), f = fixture(undefined, clock.globals); await tick();
  const posts = [], entries = new Map(); let hermes = 0;
  f.context.fetch = async (url, options) => {
    const body = JSON.parse(options.body); posts.push(body);
    if (!entries.has(body.request_id)) {hermes++; entries.set(body.request_id, 'pending');}
    return {ok:false, status:409, json:async () => ({pending:true, error:'Still sending'})};
  };
  f.run(`state.current = 'one'`); f.get('message').value = 'Keep checking';
  await f.run('submitMessage({preventDefault() {}})'); const since = f.run('state.sentMessages[0].since');
  await clock.advance(60000); await f.run('retryMessage(state.sentMessages[0])');
  await clock.advance(65000);
  assert.equal(f.run('state.sentMessages[0].since'), since);
  assert.equal(hermes, 1); assert.equal(clock.checks(), 0);
  assert.ok(posts.length > 10);
  const count = posts.length; await clock.advance(60000); assert.equal(posts.length, count);
  assert.match(text(f.get('transcript')), /Check again/);
});

test('sign-out cancels pending timers and a delayed callback cannot post into a new sign-in', async () => {
  const clock = pendingClock(), f = fixture(undefined, clock.globals); await tick();
  f.run(`state.current = 'one'; globalThis.posts = 0; api = async () => {posts++; const error = new Error('pending'); error.pending = true; throw error;};`);
  f.get('message').value = 'Pending'; await f.run('submitMessage({preventDefault() {}})');
  const callback = [...clock.timers.values()].find(timer => timer.ms === 5000)?.fn;
  assert.ok(callback); f.run('signedOut()'); assert.equal(clock.checks(), 0);
  await callback(); await clock.advance(15000); assert.equal(f.run('posts'), 1);
});

test('saving one pending row preserves the other item and its active timer', async () => {
  const clock = pendingClock(), f = fixture(undefined, clock.globals); await tick();
  f.run(`state.current = 'one'; globalThis.posts = []; api = async (path, options) => {
    const body = JSON.parse(options.body); posts.push(body); const error = new Error('Still sending'); error.pending = true; throw error;
  };`);
  for (const value of ['First', 'Second']) {
    f.get('message').value = value; await f.run('submitMessage({preventDefault() {}})');
  }
  const second = f.run('state.sentMessages[1]');
  f.run(`renderMessages([{id:1, role:'user', content:'First'}]);`);
  assert.equal(f.run('state.sentMessages[0]'), second); assert.equal(clock.checks(), 1);
  const own = f.get('transcript').children.filter(n => n.className === 'message from-you');
  assert.equal(own.length, 2);
  await clock.advance(5000); assert.equal(f.run('posts.length'), 3);
  assert.equal(f.run('posts.at(-1).text'), 'Second');
  assert.equal(f.run('posts[1].request_id'), f.run('posts[2].request_id'));
  const remove = f.get('transcript').children.flatMap(n => n.children).find(n => n.textContent === 'Delete');
  remove.events.click(); assert.equal(clock.checks(), 0);
  await clock.advance(15000); assert.equal(f.run('posts.length'), 3);
});

test('loading earlier messages prepends rows without moving the reader, and polls retain them', async () => {
  const f = fixture(); await tick();
  const transcript = f.get('transcript');
  Object.defineProperty(transcript, 'scrollHeight', {get() { return this.children.length * 100; }});
  transcript.clientHeight = 100;
  f.run(`state.current = 'one'; globalThis.calls = [];
    api = async path => {
      calls.push(path);
      if (path.endsWith('/approvals')) return {approvals:[]};
      if (path.includes('?before=')) return {messages:[{id:1,role:'user',content:'earlier'}],has_older:false};
      return {messages:[{id:2,role:'assistant',content:'newest'}],has_older:true};
    };`);
  await f.run('loadMessages()');
  const button = transcript.children[0];
  assert.equal(button.textContent, 'Load earlier messages');
  transcript.scrollTop = 35;
  const height = transcript.scrollHeight;
  await f.run('loadEarlierMessages()');
  assert.equal(transcript.scrollTop, 35 + transcript.scrollHeight - height);
  assert.deepEqual(Array.from(f.run('state.lastRows.map(row => row.id)')), [1,2]);
  assert.ok(f.run(`calls.includes('/conversations/one?before=2&limit=500')`));
  f.run(`api = async path => path.endsWith('/approvals') ? {approvals:[]} :
    {messages:[{id:2,role:'assistant',content:'updated'},{id:3,role:'user',content:'next'}],has_older:true};`);
  const top = transcript.scrollTop;
  await f.run('loadMessages()');
  assert.deepEqual(Array.from(f.run('state.lastRows.map(row => row.id)')), [1,2,3]);
  assert.equal(f.run('state.lastRows[1].content'), 'updated');
  assert.equal(transcript.scrollTop, top);
  assert.equal(transcript.children.some(n => n.textContent === 'Load earlier messages'), false);
  f.run('signedOut()');
  assert.equal(f.run('state.messageHistory'), null);
});

test('an older history page cannot follow the reader to another conversation', async () => {
  const f = fixture(); await tick();
  f.run(`state.current = 'one';
    api = async path => path.endsWith('/approvals') ? {approvals:[]} :
      {messages:[{id:2,role:'user',content:'one'}],has_older:true};`);
  await f.run('loadMessages()');
  f.run(`api = () => new Promise(resolve => { globalThis.finishHistory = resolve; });`);
  const pending = f.run('loadEarlierMessages()');
  f.run(`api = async path => path.endsWith('/approvals') ? {approvals:[]} :
    path.endsWith('/settings') ? {} : {messages:[{id:5,role:'user',content:'two'}],has_older:false};`);
  await f.run(`selectConversation('two')`);
  f.run(`finishHistory({messages:[{id:1,role:'user',content:'private old'}],has_older:false})`);
  await pending;
  assert.deepEqual(Array.from(f.run('state.lastRows.map(row => row.id)')), [5]);
  assert.equal(f.run('state.messageHistory.id'), 'two');
});

test('a history page arriving during selection preserves position when selection clears', async () => {
  const f = fixture(); await tick();
  const transcript = f.get('transcript');
  Object.defineProperty(transcript, 'scrollHeight', {get() { return this.children.length * 100; }});
  transcript.clientHeight = 100;
  f.run(`state.current = 'one'; globalThis.selected = false;
    window.getSelection = () => ({isCollapsed:!selected,rangeCount:1,getRangeAt:() => ({intersectsNode:() => true})});
    api = async path => path.endsWith('/approvals') ? {approvals:[]} :
      {messages:[{id:2,role:'user',content:'newest'}],has_older:true};`);
  await f.run('loadMessages()');
  transcript.scrollTop = 25;
  const height = transcript.scrollHeight;
  f.run(`api = () => new Promise(resolve => { globalThis.finishHistory = resolve; });`);
  const pending = f.run('loadEarlierMessages()');
  f.run(`selected = true; finishHistory({messages:[{id:1,role:'user',content:'earlier'}],has_older:false})`);
  await pending;
  assert.equal(transcript.scrollHeight, height);
  f.run('selected = false; document.events.selectionchange()');
  assert.equal(transcript.scrollTop, 25 + transcript.scrollHeight - height);
});

test('scrolling to the transcript top loads one history page at a time and can retry', async () => {
  const f = fixture(); await tick();
  f.run(`state.current = 'one';
    api = async path => path.endsWith('/approvals') ? {approvals:[]} :
      {messages:[{id:2,role:'user',content:'newest'}],has_older:true};`);
  await f.run('loadMessages()');
  f.run(`globalThis.historyCalls = 0;
    api = () => { historyCalls++; return new Promise((resolve,reject) => { globalThis.finishHistory = resolve; globalThis.failHistory = reject; }); };`);
  f.get('transcript').scrollTop = 0;
  f.get('transcript').events.scroll(); f.get('transcript').events.scroll();
  assert.equal(f.run('historyCalls'), 1);
  f.run(`failHistory(new Error('offline'))`); await tick();
  assert.equal(f.run('state.messageHistory.loading'), false);
  f.get('transcript').events.scroll();
  assert.equal(f.run('historyCalls'), 2);
  f.run(`finishHistory({messages:[],has_older:false})`); await tick();
  f.get('transcript').events.scroll();
  assert.equal(f.run('historyCalls'), 2);
});

test('identical transcript polls keep the DOM and skip Markdown, while visible changes render', async () => {
  const f = fixture(); await tick();
  const transcript = f.get('transcript'), replace = transcript.replaceChildren.bind(transcript);
  let renders = 0;
  transcript.replaceChildren = (...rows) => { renders++; replace(...rows); };
  f.run(`state.current = 'one'; globalThis.markdowns = 0; const markdown = addMarkdown;
    addMarkdown = (...args) => {markdowns++; markdown(...args);};
    globalThis.data = {messages:[{id:1,role:'assistant',content:'Reply',reasoning:'Thoughts',timestamp:1700000000}],has_older:true};
    api = async path => path.endsWith('/approvals') ? {approvals:data.approvals || []} : JSON.parse(JSON.stringify(data));`);
  await f.run('loadMessages()');
  const body = transcript.children.find(n => n.className === 'message from-agent');
  const markdowns = f.run('markdowns');
  await f.run('loadMessages()'); await f.run('loadMessages()');
  assert.equal(renders, 1); assert.equal(f.run('markdowns'), markdowns);
  assert.ok(transcript.children.includes(body));
  for (const change of [
    `data.messages[0].content = 'Changed reply'`,
    `data.messages[0].reasoning = 'Changed thoughts'`,
    `data.messages[0].roost_usage = {input_tokens:10,output_tokens:20,model_seconds:2}`,
    `data.has_older = false`,
    `data.working = true; data.after = 1; data.started = 2`,
    `state.reasoning = 'high'`,
    `state.playing = {conversation:'one',id:1}`,
    `data.approvals = state.approvals = [{request_id:'approval'}]`,
  ]) {
    const before = renders;
    f.run(change); await f.run('loadMessages()');
    assert.equal(renders, before + 1, change);
    await f.run('loadMessages()'); assert.equal(renders, before + 1, 'the next identical poll is skipped');
  }
  f.run(`loadReasoning = () => {};`);
  await f.run(`selectConversation('one')`);
  assert.ok(transcript.children.some(n => n.className === 'message from-agent'), 'reselecting replaces the loading view');
});

test('Retry state and older-history prepend invalidate the transcript without rebuilding on the next poll', async () => {
  const f = fixture(); await tick();
  const transcript = f.get('transcript'), replace = transcript.replaceChildren.bind(transcript);
  let renders = 0;
  transcript.replaceChildren = (...rows) => { renders++; replace(...rows); };
  f.run(`state.current = 'one';
    state.sentMessages = [{id:'one',key:'failed',text:'Try again',since:Date.now(),after:2,failed:true}];
    api = async path => path.endsWith('/approvals') ? {approvals:[]} : path.includes('?before=')
      ? {messages:[{id:1,role:'user',content:'Earlier'}],has_older:false}
      : {messages:[{id:2,role:'assistant',content:'Newest'}],has_older:true};`);
  await f.run('loadMessages()'); await f.run('loadMessages()'); assert.equal(renders, 1);
  const actions = () => transcript.children.flatMap(n => n.children).filter(n => n.className === 'send-action');
  assert.equal(actions()[0].textContent, 'Retry');
  f.run('state.sentMessages[0].retrying = true'); await f.run('loadMessages()');
  assert.equal(renders, 2); assert.equal(actions()[0].textContent, 'Checking…'); assert.equal(actions()[0].disabled, true);
  f.run('state.sentMessages[0].retrying = false; state.sentMessages[0].pending = true'); await f.run('loadMessages()');
  assert.equal(renders, 3); assert.equal(actions()[0].textContent, 'Check again');
  await f.run('loadMessages()'); assert.equal(renders, 3);
  await f.run('loadEarlierMessages()'); assert.equal(renders, 4);
  assert.deepEqual(Array.from(f.run('state.lastRows.map(row => row.id)')), [1,2]);
  await f.run('loadMessages()'); assert.equal(renders, 4);
  f.run('state.messageHistory.loading = true'); await f.run('loadMessages()'); assert.equal(renders, 5);
});

test('identical conversation refreshes preserve rows and selection, search and loading changes render', async () => {
  const f = fixture(); await tick();
  const list = f.get('conversation-list'), replace = list.replaceChildren.bind(list);
  let renders = 0;
  list.replaceChildren = (...rows) => { renders++; replace(...rows); };
  f.run(`state.current = 'one'; api = async () => ({sessions:[{id:'one',title:'One',preview:'Same'}],total:1});`);
  await f.run('loadConversations()'); const row = list.children[1];
  await f.run('loadConversations()'); assert.equal(renders, 1); assert.equal(list.children[1], row);
  for (const change of [`state.current = 'two'`, `state.search = 'absent'`, `state.loadingMore = true`,
    `state.conversations[0].preview = 'Changed'`, `state.working = true`, `state.timeZone = 'UTC'`]) {
    const before = renders;
    f.run(change + '; renderConversations()'); assert.equal(renders, before + 1, change);
    f.run('renderConversations()'); assert.equal(renders, before + 1);
  }
});

test('hidden pages skip transcript, conversation and job polls and still report presence', async () => {
  const polls = new Map(), f = fixture(undefined, {setInterval(fn, ms) { polls.set(ms, fn); }}); await tick();
  f.run(`show('app',true); state.current='one'; globalThis.loads=[];
    loadMessages=async()=>loads.push('chat'); loadConversations=async()=>loads.push('list'); loadJobs=async()=>loads.push('jobs');
    fetch=async path=>{if(path==='/api/presence') loads.push('presence'); return {};}; document.hidden=true;`);
  polls.get(3000)(); polls.get(20000)();
  f.run(`state.section='jobs'`); polls.get(20000)(); polls.get(15000)(); await tick();
  assert.deepEqual(Array.from(f.run('loads')), ['presence']);
  f.run(`document.hidden=false; state.section='messages'`); polls.get(3000)(); polls.get(20000)(); await tick();
  assert.deepEqual(Array.from(f.run('loads')), ['presence','chat','list']);
});

test('healthy live streams poll every 15 seconds and errors, completion and finished turns restore fast polling', async () => {
  const clock = pendingClock(), {opened, globals} = eventSources(), polls = new Map();
  const f = fixture(undefined, {...clock.globals, ...globals, setInterval(fn, ms) { polls.set(ms, fn); }}); await tick();
  f.run(`show('app',true); state.current='one'; globalThis.loads=0; globalThis.active=true;
    api=async path=>path.endsWith('/approvals') ? {approvals:[]} :
      (loads++, {messages:[],working:active,live:active,after:0,started:1});`);
  const poll = async () => { await clock.advance(3000); polls.get(3000)(); await tick(); };
  await f.run('loadMessages()');
  await poll(); assert.equal(f.run('loads'), 2, 'an unopened stream keeps the fast poll');
  opened[0].onopen();
  opened[0].send({reset:true,started:1,user:'Question',text:'Live',thoughts:'Thinking'});
  for (let i=0; i<4; i++) await poll();
  assert.equal(f.run('loads'), 2);
  await poll(); assert.equal(f.run('loads'), 3);
  opened[0].readyState=0; opened[0].onerror(); await poll();
  assert.equal(f.run('loads'), 4, 'a reconnecting stream restores fast polls');
  opened[0].readyState=1; opened[0].onopen(); await poll(); assert.equal(f.run('loads'), 4);
  f.run('active=false'); opened[0].send({done:true}); await tick();
  assert.equal(f.run('loads'), 5, 'completion refreshes immediately');
  await poll(); assert.equal(f.run('loads'), 6);
  f.run(`active=true; openStream('one')`); opened[1].onopen();
  await poll(); assert.equal(f.run('loads'), 6, 'a newly healthy stream slows polling immediately');
  await f.run('loadMessages()');
  f.run('active=false'); await clock.advance(15000); polls.get(3000)(); await tick();
  const finished = f.run('loads'); await poll();
  assert.equal(f.run('loads'), finished + 1, 'a finished server turn restores fast polls even without a done event');
  f.run(`active=false; openStream('one')`);
  opened[1].readyState=2; opened[1].onerror(); await tick();
  assert.equal(f.run('state.stream'), null, 'a closed stream ends');
});

test('idle conversations slow after a minute and send, pending rows and visibility return restore fast polling', async () => {
  const clock = pendingClock(), polls = new Map();
  const f = fixture(undefined, {...clock.globals, setInterval(fn, ms) { polls.set(ms, fn); }}); await tick();
  f.run(`show('app',true); state.current='one'; globalThis.loads=0;
    loadConversations=async()=>{};
    api=async (path,options)=>options?.method==='POST' ? {session_id:'one'} : path.endsWith('/approvals') ? {approvals:[]} :
      (loads++, {messages:[],working:false,live:false});`);
  const poll = async () => { await clock.advance(3000); polls.get(3000)(); await tick(); };
  await f.run('loadMessages()');
  for (let i=0; i<20; i++) await poll();
  assert.equal(f.run('loads'), 21, 'polls stay fast through the first minute');
  for (let i=0; i<3; i++) await poll();
  assert.equal(f.run('loads'), 21);
  await poll(); assert.equal(f.run('loads'), 22, 'the 3-second timer rounds the idle interval up to 12 seconds');
  await f.run(`sendText('New question')`); await tick();
  const sent = f.run('loads'); await poll(); assert.equal(f.run('loads'), sent + 1);
  f.run(`state.sentMessages=[{id:'one',key:'pending',text:'Pending',since:Date.now(),after:0,failed:true,pending:true}]; state.idleSince=Date.now()-120000;`);
  await poll(); assert.equal(f.run('loads'), sent + 2, 'an ambiguous pending send keeps fast polling');
  f.run(`state.sentMessages=[]; state.idleSince=Date.now()-120000;`);
  await poll(); assert.equal(f.run('loads'), sent + 2);
  f.context.document.visibilityState='visible'; f.context.document.events.visibilitychange(); await tick();
  assert.equal(f.run('loads'), sent + 3, 'visibility return refreshes immediately');
  await poll(); assert.equal(f.run('loads'), sent + 4);
});

function paste(f, text, start = f.get('message').value.length, end = start) {
  const input = f.get('message');
  input.selectionStart = start; input.selectionEnd = end;
  input.setRangeText = (value, from, to) => {
    input.value = input.value.slice(0, from) + value + input.value.slice(to);
    input.selectionStart = input.selectionEnd = from + value.length;
  };
  let prevented = false;
  input.events.paste({clipboardData: {getData: () => text}, preventDefault() {prevented = true;}});
  return prevented;
}

test('long pastes fold at either threshold and replace the selection; short pastes stay native', async () => {
  const f = fixture(); await tick();
  assert.equal(paste(f, 'short\npaste'), false);
  assert.equal(paste(f, 'x'.repeat(799)), false);
  f.get('message').value = 'Before replace after';
  const lines = Array.from({length:8}, (_, i) => `line ${i + 1}`).join('\r\n');
  assert.equal(paste(f, lines, 7, 14), true);
  assert.equal(f.get('message').value, 'Before [Pasted text #1 · 8 lines] after');
  assert.equal(paste(f, 'x'.repeat(800)), true);
  assert.match(f.get('message').value, /\[Pasted text #2 · 1 lines\]/);
  assert.equal(f.run('draftPastes().length'), 2);
});

test('sending expands multiple tokens exactly once in the POST body', async () => {
  const posts = [], f = fixture(async (url, options = {}) => {
    if (options.method === 'POST') { posts.push(JSON.parse(options.body)); return {ok:true, json:async () => ({session_id:'one'})}; }
    return {ok:true, json:async () => ({signed_in:false})};
  }); await tick();
  f.run(`state.current = 'one'; loadMessages = async () => {}; loadConversations = async () => {};`);
  const first = 'First\n'.repeat(8) + '[Pasted text #2 · 1 lines]', second = 'x'.repeat(800);
  f.get('message').value = 'Review: ';
  paste(f, first); paste(f, second);
  await f.run('submitMessage({preventDefault() {}})');
  assert.equal(posts[0].text, 'Review: ' + first + second);
  assert.equal(f.run('state.sentMessages[0].text'), posts[0].text);
  assert.equal(f.run('draftPastes().length'), 0);
});

test('deleting a token drops its content while keeping other folded pastes', async () => {
  const f = fixture(); await tick();
  paste(f, 'first\n'.repeat(8)); const token = f.get('message').value;
  paste(f, 'second\n'.repeat(8));
  f.get('message').value = f.get('message').value.replace(token, ''); f.get('message').events.input();
  assert.equal(f.run('draftPastes().length'), 1);
  assert.equal(f.run('state.composerDrafts[0].pastes.length'), 1);
  f.get('message').value = ''; f.get('message').events.input();
  assert.equal(f.run('state.composerDrafts.length'), 0);
});

test('folded pastes survive conversation switches and a stored draft reload', async () => {
  const stored = new Map(), localStorage = {getItem: key => stored.get(key),
    setItem: (key, value) => stored.set(key, value), removeItem: key => stored.delete(key)};
  const f = fixture(undefined, {localStorage}); await tick();
  f.run(`loadMessages = async () => {}; loadReasoning = () => {};`);
  await f.run(`selectConversation('one')`);
  const full = 'saved\n'.repeat(8); paste(f, full); const token = f.get('message').value;
  await f.run(`selectConversation('two')`); assert.equal(f.run('draftPastes().length'), 0);
  await f.run(`selectConversation('one')`); assert.equal(f.get('message').value, token);
  const reloaded = fixture(() => new Promise(() => {}), {localStorage});
  reloaded.run(`loadMessages = async () => {}; loadReasoning = () => {};`);
  await reloaded.run(`selectConversation('one')`);
  assert.equal(reloaded.get('message').value, token);
  assert.equal(reloaded.run('expandPastes($("message").value)'), full);
  reloaded.run(`globalThis.post = null; api = async (path, options) => {post = JSON.parse(options.body); return {session_id:'one'};}; loadConversations = async () => {};`);
  await reloaded.run('submitMessage({preventDefault() {}})');
  assert.equal(reloaded.run('post.text'), full.trim());
});

test('Retry resends expanded pasted text without consuming a newer folded draft', async () => {
  const f = fixture(); await tick();
  f.run(`state.current = 'one'; globalThis.posts = []; api = async (path, options) => {
    posts.push(JSON.parse(options.body).text); throw new Error('rejected'); };`);
  const full = 'pasted\n'.repeat(8); paste(f, full);
  await f.run('submitMessage({preventDefault() {}})');
  assert.match(text(f.get('transcript')), /Not sent/);
  paste(f, 'next\n'.repeat(8)); const next = f.get('message').value;
  f.run(`api = async (path, options) => {posts.push(JSON.parse(options.body).text); return {session_id:'one'};};
    loadConversations = async () => {}; loadMessages = async () => {};`);
  await f.run('retryMessage(state.sentMessages[0])');
  assert.deepEqual(Array.from(f.run('posts')), [full.trim(), full.trim()]);
  assert.equal(f.get('message').value, next);
  assert.equal(f.run('draftPastes().length'), 1);
});

test('long user history folds, toggles accessibly and stays expanded through redraws', async () => {
  const f = fixture(); await tick();
  f.context.full = Array.from({length:10}, (_, i) => `Line ${i + 1}`).join('\n');
  f.run(`state.current = 'one'; globalThis.rows = [{id:1, role:'user', content:full}]; renderMessages(rows);`);
  const body = () => f.get('transcript').children.find(n => n.className === 'message from-you').children[0];
  let button = body().children[1];
  const attributes = {}; button.setAttribute = (key, value) => {attributes[key] = value;};
  assert.equal(button.textContent, 'Show all 10 lines');
  assert.doesNotMatch(text(body()), /Line 4/);
  let stopped = false;
  button.events.click({stopPropagation() {stopped = true;}});
  assert.equal(stopped, true); assert.equal(attributes['aria-expanded'], 'true');
  assert.match(text(body()), /Line 10/); assert.equal(button.textContent, 'Show less');
  f.run(`rows.push({id:2,role:'assistant',content:'Reply'}); renderMessages(rows);`);
  assert.equal(body().children[1].textContent, 'Show less');
  button = body().children[1]; button.events.click({stopPropagation() {}});
  assert.equal(button.textContent, 'Show all 10 lines'); assert.doesNotMatch(text(body()), /Line 4/);
  f.run(`state.current = 'two'; renderMessages(rows)`);
  assert.equal(body().children[1].textContent, 'Show all 10 lines');
  f.run(`renderMessages([{id:1,role:'user',content:'x'.repeat(800)}])`);
  assert.equal(body().children[1].textContent, 'Show all 1 lines');
  assert.ok(text(body().children[0]).length < 450);
});

test('folding cannot bypass the expanded message length limit', async () => {
  const f = fixture(); await tick();
  paste(f, 'x'.repeat(16000)); const token = f.get('message').value;
  assert.equal(paste(f, 'y'.repeat(800)), true);
  assert.equal(f.get('message').value, token);
  assert.equal(f.run('draftPastes().length'), 1);
  f.get('message').value += 'extra';
  f.run(`api = () => {throw new Error('must not send');};`);
  await f.run('submitMessage({preventDefault() {}})');
  assert.equal(f.get('message').value, token + 'extra');
  assert.equal(f.run('state.sentMessages.length'), 0);
});

test('a new conversation migrates its folded follow-up draft after navigation', async () => {
  const f = fixture(); await tick();
  f.run(`api = () => new Promise(resolve => {globalThis.finish = resolve;});
    loadMessages = async () => {}; loadConversations = async () => {}; loadReasoning = () => {};`);
  f.get('message').value = 'First message';
  const sending = f.run('submitMessage({preventDefault() {}})');
  const full = 'follow-up\n'.repeat(8); paste(f, full); const token = f.get('message').value;
  await f.run(`selectConversation('other')`);
  f.run(`finish({session_id:'created'})`); await sending;
  assert.equal(f.run('state.current'), 'other');
  await f.run(`selectConversation('created')`);
  assert.equal(f.get('message').value, token);
  assert.equal(f.run('expandPastes($("message").value)'), full);
});

test('native history uses execution ordinals and user before assistant after send now', async () => {
  const f = fixture(); await tick();
  const order = f.run(`mergeMessages(
    [{id:1,ordinal:1,role:'user'}, {id:2,ordinal:1,role:'assistant'}, {id:3,ordinal:2,role:'user'}, {id:4,ordinal:2,role:'assistant'}],
    [{id:6,ordinal:2,role:'assistant'}, {id:5,ordinal:2,role:'user'}, {id:4,ordinal:3,role:'assistant'}, {id:3,ordinal:3,role:'user'}]
  ).map(row => row.id)`);
  assert.deepEqual(Array.from(order), [1,2,5,6,3,4]);
});

test('native authoritative tails remove cancelled and edited rows while retaining earlier pages', async () => {
  const f = fixture(); await tick();
  const order = f.run(`mergeMessages(
    [{id:1,ordinal:1,role:'user'}, {id:2,ordinal:1,role:'assistant'}, {id:3,ordinal:2,role:'user'}, {id:4,ordinal:2,role:'assistant'}, {id:5,ordinal:3,role:'user'}],
    [{id:7,ordinal:2,role:'user'}, {id:8,ordinal:2,role:'assistant'}], true).map(row => row.id)`);
  assert.deepEqual(Array.from(order), [1,2,7,8]);
  assert.equal(f.run('mergeMessages([{id:1,ordinal:1}], [], true).length'), 0);
});

test('native live overlay replaces only its turn regardless of allocated message IDs', async () => {
  const f = fixture(); await tick();
  const view = f.run(`conversationView({current:'one',sent:[],server:{working:true,after:2},
    rows:[{id:1,role:'user',turn_id:'a',ordinal:1,content:'first'}, {id:2,role:'assistant',turn_id:'a',ordinal:1,content:'partial',state:'interrupted'},
      {id:5,role:'user',turn_id:'c',ordinal:2,content:'now'}, {id:6,role:'assistant',turn_id:'c',ordinal:2,content:'old',state:'running',revision:1},
      {id:3,role:'user',turn_id:'b',ordinal:3,content:'queued'}, {id:4,role:'assistant',turn_id:'b',ordinal:3,content:'',state:'queued'}],
    live:{id:'one',turn_id:'c',revision:2,text:'new',done:false}})`);
  assert.deepEqual(Array.from(view.rows, row => row.id), [1,2,5,3,4]);
  assert.equal(view.liveAfter.id, 5);
});

test('native SSE full snapshots replace text, thoughts and turn identity', async () => {
  const f = fixture(); await tick();
  const live = f.run(`applyStreamFrame(applyStreamFrame(null,
    {reset:true,turn_id:'first',revision:1,text:'A',thoughts:'thinking'},'one'),
    {reset:true,turn_id:'second',revision:2,text:'AB',thoughts:'thought',done:true},'one')`);
  assert.equal(live.text, 'AB'); assert.equal(live.thoughts, 'thought');
  assert.equal(live.turn_id, 'second'); assert.equal(live.revision, 2); assert.equal(live.done, true);
  assert.equal(f.run(`applyStreamFrame({text:'A',thoughts:''},{text:'B'},'one').text`), 'AB', 'Hermes deltas remain additive');
});

test('native controls follow turn state and latest reply', async () => {
  const f = fixture(); await tick();
  for (const state of ['queued','running','completed','interrupted','failed']) {
    for (const role of ['user','assistant']) {
      f.context.row = {turn_id:'turn',state,role};
      const controls = f.run('turnControls(row, [row])');
      const settled = ['completed','interrupted','failed'].includes(state);
      assert.equal(controls.queue, role === 'user' && state === 'queued');
      assert.equal(controls.edit, role === 'user' && settled);
      assert.equal(controls.regenerate, role === 'assistant' && settled);
      assert.equal(f.run(`turnControls(row, [row, {turn_id:'later'}]).regenerate`), false);
    }
  }
});

test('native surfaces hide Hermes features while Hermes keeps them', async () => {
  const f = fixture(); await tick();
  assert.ok(Object.values(f.run(`backendSurfaces('hermes')`)).every(Boolean));
  assert.ok(Object.values(f.run(`backendSurfaces('native')`)).every(value => !value));
  f.run(`state.backend='native'; state.settings={agent_name:''}; state.push={devices:[]}; state.about={
    roost:{},runtime:{},stats:{},at:new Date(),hermes:{reachable:false}};
    renderAgentSettings($('settings-detail')); renderServiceSettings($('settings-detail')); renderNotifications($('settings-detail')); renderAbout($('settings-detail'));`);
  const texts = node => [node.textContent, ...(node.children || []).flatMap(texts)].join(' ');
  const rendered = texts(f.get('settings-detail'));
  assert.ok(!rendered.includes('SOUL.md')); assert.ok(!rendered.includes('Restart Agent'));
  assert.ok(!rendered.includes('(Hermes)')); assert.ok(!rendered.includes('Download the plugin'));
});

test('thinking picker follows the selected model or server default', async () => {
  const f = fixture(); await tick();
  f.run(`state.models=[{id:'plain',thinking_mode:'unsupported'},{id:'effort',thinking_mode:'reasoning_effort'},
    {id:'flag',thinking_mode:'template_flag'}];`);
  assert.equal(f.run(`thinkingVisible(state.models,null,'plain')`), false);
  assert.equal(f.run(`thinkingVisible(state.models,'effort','plain')`), true);
  assert.equal(f.run(`thinkingVisible(state.models,'flag','plain')`), true);
  assert.equal(f.run(`thinkingVisible(state.models,null,'effort')`), true);
  assert.equal(f.run(`thinkingVisible(state.models,'missing','effort')`), false);
});

test('native usage keeps provider TPS and time to first token instead of recomputing prefill rate', async () => {
  const f = fixture(); await tick();
  assert.equal(f.run(`replyUsage({turn_id:'one',roost_usage:{input_tokens:128,output_tokens:64,model_seconds:10,
    tokens_per_second:32,time_to_first_token:1.25}})`), '128 in / 64 out · 32.0 TPS · 1.25 s to first token');
});

test('native composer offers Stop during a running reply and Queue for a typed message', async () => {
  const f = fixture(); await tick();
  f.run(`state.backend='native';state.lastRows=[{turn_id:'current',state:'running'}];updateNativeComposer()`);
  assert.equal(f.get('send').textContent,'Stop'); assert.equal(f.get('send').type,'button');
  assert.ok(f.get('queue-send').classList.contains('hidden'));
  f.get('message').value='next message'; f.run('updateNativeComposer()');
  assert.ok(!f.get('queue-send').classList.contains('hidden'));
  // A new stream identity takes precedence over an older transcript poll.
  f.run(`state.live={turn_id:'next',done:false};`);
  assert.equal(f.run('runningTurn()'),'next');
  f.run(`state.live=null;state.lastRows=[{turn_id:'current',state:'completed'}];updateNativeComposer()`);
  assert.equal(f.get('send').type,'submit'); assert.equal(f.get('send').textContent,'↑');
});

test('native accepted sends reconcile by request identity even when IDs move backwards', async () => {
  const f=fixture();await tick();
  const view=f.run(`withSentMessages([{id:3,role:'user',content:'same',client_request_id:'request'}],
    [{id:'chat',text:'same',request_id:'request',after:9,key:'local',since:0}], 'chat')`);
  assert.equal(view.sent.length,0);assert.equal(view.savedAs.local,3);assert.equal(view.rows.length,1);
});

test('native composer states retain idle behavior and expose Stop with or without text', async () => {
  const f = fixture(); await tick();
  for (const turn of [null, 'running', 'queued']) for (const text of ['', 'next message']) {
    f.context.turnState = turn; f.get('message').value = text;
    f.get('composer').classList.toggle('has-text', Boolean(text));
    f.run(`state.backend='native'; state.live=null; state.lastRows=turnState ? [{turn_id:'one',state:turnState}] : []; updateNativeComposer()`);
    assert.equal(f.get('send').classList.contains('stopping'), turn !== null, `${turn}/${text}: Stop`);
    assert.equal(f.get('send').type, turn ? 'button' : 'submit');
    assert.equal(f.get('queue-send').classList.contains('hidden'), !(turn && text), `${turn}/${text}: Queue`);
    assert.equal(f.get('send').textContent, turn ? 'Stop' : '↑');
  }
  // The original failure lived in CSS: an aria-label and a Stop class alone did not make the button visible.
  const css = fs.readFileSync(path.join(__dirname, '../roost/static/style.css'), 'utf8');
  assert.match(css, /\.composer\s+\.send-button\.stopping\s*\{[^}]*display:\s*grid/s);
  f.run(`state.backend='hermes'; state.lastRows=[];`);
  f.get('send').textContent='Hermes send'; f.run('updateNativeComposer()');
  assert.equal(f.get('send').textContent,'Hermes send');
});

test('Stop targets a running turn, or cancels the first queued turn while waiting for admission', async () => {
  const f = fixture(); await tick();
  f.run(`globalThis.actions=[]; nativeControl=async (action,id)=>actions.push([action,id]);
    state.lastRows=[{turn_id:'first',state:'queued'},{turn_id:'second',state:'queued'}];`);
  await f.run('stopNativeReply()');
  f.run(`state.live={turn_id:'first',state:'queued',done:false}`);
  assert.equal(f.run('runningTurn()'), undefined);
  await f.run('stopNativeReply()');
  f.run(`state.lastRows.unshift({turn_id:'active',state:'running'});`);
  await f.run('stopNativeReply()');
  assert.deepEqual(JSON.parse(f.run('JSON.stringify(actions)')), [['cancel','first'],['cancel','first'],['stop','active']]);
});

test('model rows share switches, expose one detail panel, and keep the default enabled', async () => {
  const f = fixture(); await tick();
  const walk = node => [node, ...(node.children || []).flatMap(walk)];
  const nodes = () => walk(f.get('settings-detail'));
  const byClass = name => nodes().filter(node => (node.className || '').split(' ').includes(name));
  f.run(`state.backend='native';state.settings={};state.settingsGroup='models';state.primary={base_url:'',has_key:false,
    default_model:'one',models:[{id:'one',display_name:'First',enabled:true,thinking_mode:'reasoning_effort'},
      {id:'two',display_name:'',enabled:false,thinking_mode:'unsupported'}]};renderSettings()`);
  assert.equal(byClass('model-row').length,2);
  assert.equal(byClass('model-expand')[0].getAttribute('aria-expanded'),'false');
  assert.equal(nodes().filter(node => node.type === 'checkbox' || node.type === 'radio').length,0);
  assert.deepEqual(byClass('model-name').map(node => node.textContent), ['First','two']);
  assert.deepEqual(byClass('model-id').map(node => node.className), ['model-id','model-id hidden']);
  const picker = () => nodes().find(node => node.id === 'settings-default-model');
  assert.deepEqual(picker().children.map(node => node.value),['one']);
  const toggles = () => nodes().filter(node => node.getAttribute?.('role') === 'switch' && node.id !== 'settings-use-fallback');
  assert.equal(toggles().length,2);assert.equal(toggles()[0].className,'job-toggle');
  toggles()[1].events.click();
  assert.deepEqual(picker().children.map(node => node.value),['one','two']);
  byClass('model-expand')[0].events.click();
  assert.equal(f.run('state.openModel'),'one');
  assert.equal(byClass('model-expand')[0].getAttribute('aria-expanded'),'true');
  byClass('model-expand')[1].events.click();
  assert.equal(f.run('state.openModel'),'two');
  assert.equal(byClass('model-details').filter(node => !node.className.includes('hidden')).length,1);
  assert.equal(byClass('model-expand')[0].getAttribute('aria-expanded'),'false');
  const name = byClass('model-details')[1].children[0].children[0];
  name.value='Second';name.events.input();
  assert.equal(byClass('model-name')[1].textContent,'Second');
  assert.equal(picker().children[1].textContent,'Second');
  toggles()[0].events.click();
  assert.equal(f.run('state.primary.default_model'),'two');
  assert.deepEqual(picker().children.map(node => node.value),['two']);
  toggles()[1].events.click();
  assert.equal(f.run('state.primary.default_model'),null);assert.equal(picker().disabled,true);
});

test('native full snapshots replace trace and sources instead of appending', async () => {
  const f = fixture(); await tick();
  const result = f.run('(() => { const frame = {reset: true, user: "Search", text: "Answer [1]", thoughts: "Check", trace: [{kind: "reasoning", text: "Check"}, {kind: "tool", id: "call", name: "web_search", arguments: "{}", status: "running"}], sources: [{n: 1, title: "Example", url: "https://example.org"}]}; const first = applyStreamFrame(null, frame, "one"); return applyStreamFrame(first, {...frame, trace: [...frame.trace.slice(0, 1), {...frame.trace[1], status: "completed", result: "Untrusted"}]}, "one"); })()');
  assert.equal(result.text, 'Answer [1]'); assert.equal(result.trace.length, 2); assert.equal(result.sources.length, 1);
  assert.equal(result.trace[1].status, 'completed');
});

test('native Thoughts interleave tools and render bounded literal output in collapsed details', async () => {
  const f = fixture(); await tick();
  f.run('fillThoughts($("test-thoughts"), [{kind: "reasoning", text: "Before"}, {kind: "tool", id: "one", name: "web_search", arguments: JSON.stringify({query:"example"}), status: "completed", result: "<script>unsafe</script>" + "x".repeat(17000)}, {kind: "reasoning", text: "After"}]);');
  const children = f.get('test-thoughts').children;
  assert.deepEqual(children.map(node => node.tagName), ['P', 'DIV', 'P']);
  const tool = children[1], output = tool.children[1];
  assert.equal(tool.children[0].textContent, 'Searched the web for "example"');
  assert.equal(output.tagName, 'DETAILS'); assert.equal(output.open, false);
  assert.equal(output.children[1].tagName, 'PRE');
  assert(output.children[1].textContent.startsWith('<script>unsafe</script>'));
  assert(output.children[1].textContent.endsWith('[Output trimmed for display.]'));
  assert(output.children[1].textContent.length < 16100);
});

test('native citations are safe turn-specific links with a source list; code stays literal', async () => {
  const f = fixture(); await tick();
  f.run('addReply($("test-reply"), "Answer [1] **bold [2]** \u0060[1]\u0060 [9]\\n\\n\u0060\u0060\u0060text\\n[1]\\n\u0060\u0060\u0060", [{n: 1, title: "Example", url: "https://example.org/page"}, {n: 2, title: "<b>Literal</b>", url: "http://example.com/other"}, {n: 3, title: "Unsafe", url: "javascript:alert(1)"}]);');
  function descendants(node) { return [node, ...(node.children || []).flatMap(descendants)]; }
  const nodes = descendants(f.get('test-reply')), links = nodes.filter(node => node.tagName === 'A');
  assert.equal(links.length, 4);
  assert.equal(links[0].textContent, '[1]'); assert.equal(links[0].href, 'https://example.org/page');
  assert.equal(links[1].href, 'http://example.com/other');
  assert.equal(links[3].textContent, '[2] <b>Literal</b>');
  assert(links.every(link => link.rel === 'noopener noreferrer' && link.target === '_blank'));
  assert(nodes.some(node => node.tagName === 'CODE' && node.textContent === '[1]'));
  assert(nodes.some(node => node.textContent?.includes('[9]')));
  assert.equal(f.run('sourceLink({url:"https://REPLACE_USER:REPLACE_PASSWORD@example.org"}, "bad")'), null);
});

test('globe follows native search, model tools and No web, and resets on send', async () => {
  const f = fixture(); await tick();
  f.run('state.backend = "native"; state.models = [{id: "example", tools: true}]; state.defaultModel = "example"; state.webAvailable = true; state.noWeb = false; updateWebToggle();');
  assert.equal(f.get('web-toggle').classList.contains('hidden'), false);
  f.run('state.noWeb = true; updateWebToggle();');
  assert.equal(f.get('web-toggle').classList.contains('hidden'), true);
  f.run('state.noWeb = false; state.models[0].tools = false; updateWebToggle();');
  assert.equal(f.get('web-toggle').classList.contains('hidden'), true);
  f.run('state.models[0].tools = true; state.webAvailable = false; updateWebToggle();');
  assert.equal(f.get('web-toggle').classList.contains('hidden'), true);
  f.run('state.webAvailable = true; state.webForce = true; state.current = "one"; globalThis.payloads = []; api = async (path, options) => { payloads.push(JSON.parse(options.body)); return {session_id:"one"}; }; loadConversations = async () => {}; loadMessages = async () => {};');
  await f.run('sendText("Search now")');
  assert.equal(f.run('payloads[0].web'), 'force');
  assert.equal(f.run('state.webForce'), false);
  assert.equal(f.get('web-toggle').getAttribute('aria-pressed'), 'false');
  f.run('state.backend = "hermes"; updateWebToggle();');
  assert.equal(f.get('web-toggle').classList.contains('hidden'), true);
});


test('built-in tool labels describe progress, failures and safe page links', async () => {
  const f = fixture(); await tick();
  for (const [name, argument, status, expected] of [
    ['web_search', 'example', 'running', 'Searching the web for "example"'],
    ['web_search', 'example', 'error', 'Search failed for "example"'],
    ['web_search', 'example', 'interrupted', 'Search interrupted for "example"'],
    ['read_page', 'https://example.org/page', 'running', 'Reading example.org'],
    ['read_page', 'https://example.org/page', 'completed', 'Read example.org'],
    ['read_page', 'https://example.org/page', 'error', 'Read failed for example.org'],
    ['read_page', 'https://example.org/page', 'interrupted', 'Read interrupted for example.org'],
    ['read_page', 'javascript:alert(1)', 'error', 'Read failed for javascript:alert(1)'],
    ['custom', 'example', 'completed', 'custom · example · completed'],
  ]) {
    f.context.item = {kind:'tool', id:'one', name, arguments:JSON.stringify(name === 'read_page' ? {url:argument} : {query:argument}), status};
    f.run('fillThoughts($("labels"), [item])');
    const label = f.get('labels').children[0].children[0];
    assert.equal(label.textContent, expected);
    const links = label.children.filter(node => node.tagName === 'A');
    assert.equal(links.length, name === 'read_page' && argument.startsWith('https:') ? 1 : 0);
    if (links.length) {
      assert.equal(links[0].href, argument); assert.equal(links[0].textContent, argument);
      assert.equal(links[0].rel, 'noopener noreferrer');
    }
  }
});

test('globe has a named 44px coarse-pointer target and reflects toggle state', async () => {
  const css = fs.readFileSync(path.join(__dirname, '../roost/static/style.css'), 'utf8');
  assert.match(css, /@media \(pointer: coarse\)\s*\{[^}]*\.web-toggle[^}]*min-width: 44px;[^}]*min-height: 44px;/);
  const html = fs.readFileSync(path.join(__dirname, '../roost/static/index.html'), 'utf8');
  assert.match(html, /id="web-toggle"[^>]*aria-label="[^"]+"[^>]*aria-pressed="false"/);
  const f = fixture(); await tick();
  f.run('state.backend = "native"; state.models = [{id: "example", tools: true}]; state.defaultModel = "example"; state.webAvailable = true; updateWebToggle();');
  f.get('web-toggle').events.click();
  assert.equal(f.get('web-toggle').getAttribute('aria-pressed'), 'true');
  f.get('web-toggle').events.click();
  assert.equal(f.get('web-toggle').getAttribute('aria-pressed'), 'false');
});

test('native persona names label the header and each reply while Hermes keeps its agent name', async () => {
  const f = fixture(); await tick();
  f.run(`state.backend='native';state.agentName='Old name';state.currentPersona='Guide';`);
  assert.equal(f.run('agent()'), 'Guide');
  assert.equal(f.run(`replyName({persona_name:'Builder'})`), 'Builder');
  f.run(`state.current='chat';state.turn={id:'chat',working:false};renderMessages([
    {id:1,turn_id:'turn',ordinal:1,role:'user',state:'completed',content:'Question'},
    {id:2,turn_id:'turn',ordinal:1,role:'assistant',state:'completed',content:'Answer',persona_name:'Builder',trace:[],memories:[]}
  ])`);
  const walk = node => [node, ...(node.children || []).flatMap(walk)];
  assert.equal(walk(f.get('transcript')).find(node => node.className === 'persona-label').textContent, 'Builder');
  assert.equal(walk(f.get('transcript')).filter(node => node.textContent === 'Remember…').length, 2);
  f.run(`state.backend='hermes';state.agentName='Remote assistant'`);
  assert.equal(f.run('agent()'), 'Remote assistant');
  assert.equal(f.run(`replyName({persona_name:'Builder'})`), 'Remote assistant');
});

test('native settings offer Persona and Memory and hide the agent-name field', async () => {
  const f = fixture(); await tick();
  f.run(`state.backend='native';state.settings={};renderAgentSettings($('agent-settings'));`);
  assert.equal(f.get('agent-settings').children.length, 0);
  assert.ok(f.run(`settingsGroups().some(row=>row[0]==='personas')`));
  assert.ok(f.run(`settingsGroups().some(row=>row[0]==='memory')`));
  assert.ok(!f.run(`settingsGroups().some(row=>row[0]==='agent')`));
  f.run(`state.personaData={default_persona_id:'default',personas:[{id:'default',name:'Assistant'},{id:'guide',name:'Guide'}]};fillPersonaPicker($('picker'),'guide')`);
  assert.deepEqual(f.get('picker').children.map(node => node.textContent), ['Default (Assistant)', 'Assistant', 'Guide']);
  assert.equal(f.get('picker').value, 'guide');
  f.run(`state.backend='hermes'`);
  assert.ok(!f.run(`settingsGroups().some(row=>['personas','memory'].includes(row[0]))`));
});

test('memory meter uses server counts and warns only above budget with origin tags', async () => {
  const f = fixture(); await tick();
  const walk = node => [node, ...(node.children || []).flatMap(walk)];
  for (const count of [2000, 2001]) {
    f.context.count = count;
    f.run(`state.memoryData={facts:[{id:1,text:'A fact',origin:'model'},{id:2,text:'Another fact',origin:'user'}],used_tokens:count,budget_tokens:2000};$('memory-detail').replaceChildren();renderMemory($('memory-detail'))`);
    const nodes = walk(f.get('memory-detail'));
    assert.equal(nodes.find(node => node.className?.includes('memory-meter')).textContent, `~${count.toLocaleString()} / 2,000 tokens`);
    assert.equal(nodes.filter(node => node.className === 'memory-warning').length, count > 2000 ? 1 : 0);
    assert.deepEqual(nodes.filter(node => node.className === 'memory-origin').map(node => node.textContent), ['Model', 'You']);
  }
});

test('Remember sheet prefills selection only from its message, caps text, and saves user source links', async () => {
  const f = fixture(); await tick();
  f.run(`globalThis.row={id:7,turn_id:'turn',role:'assistant',state:'completed',content:'Full message'};
    globalThis.body={contains: node=>node==='inside'};state.current='chat';
    window.getSelection=()=>({isCollapsed:false,anchorNode:'inside',focusNode:'inside',toString:()=> 'Selected fact'});
    appendRemember($('actions'),row,body)`);
  assert.equal(f.run('rememberPrefill(row, body)'), 'Selected fact');
  f.run(`window.getSelection=()=>({isCollapsed:false,anchorNode:'outside',focusNode:'inside',toString:()=> 'Wrong fact'})`);
  assert.equal(f.run('rememberPrefill(row, body)'), 'Full message');
  f.run(`window.getSelection=()=>'';row.content='x'.repeat(600)`);
  assert.equal(f.run('rememberPrefill(row, body).length'), 500);
  f.get('actions').children[0].events.click();
  assert.equal(f.get('remember-sheet').open, true);
  assert.equal(f.get('remember-text').value.length, 500);
  f.run(`globalThis.saved=null;api=async(path,options)=>{saved=JSON.parse(options.body);return {facts:[],used_tokens:2001,budget_tokens:2000};}`);
  f.get('remember-text').value = 'Edited fact';
  await f.run('saveRemember({preventDefault(){}})');
  assert.deepEqual(JSON.parse(f.run('JSON.stringify(saved)')), {text:'Edited fact',source_message_id:7,conversation_id:'chat'});
  assert.equal(f.get('remember-sheet').open, false);
  assert.equal(f.run('state.rememberBusy'), false);
  f.run(`$('pending-actions').replaceChildren();appendRemember($('pending-actions'),{...row,state:'running'},body)`);
  assert.equal(f.get('pending-actions').children.length, 0);
});

test('Thoughts omit an empty memory snapshot and pluralise the used-memories summary', async () => {
  const f = fixture(); await tick();
  f.run(`state.backend='native';globalThis.none=nativeThoughts({turn_id:'t',memories:[],trace:[]});globalThis.two=nativeThoughts({turn_id:'t',memories:[{id:1,text:'A'},{id:2,text:'B'}],trace:[]});fillThoughts($('thoughts'),two)`);
  assert.equal(f.run(`none.length`), 0);
  assert.equal(f.get('thoughts').children[0].children[0].textContent, 'Used 2 memories');
});

test('Thoughts show collapsed memory snapshots and friendly remembered and forgotten steps', async () => {
  const f = fixture(); await tick();
  f.run(`state.backend='native';globalThis.items=nativeThoughts({turn_id:'turn',memories:[{id:1,text:'Deleted later'}],trace:[
    {kind:'tool',id:'save',name:'remember',arguments:'{"fact":"A lasting preference"}',status:'completed',fact_id:2,fact:'A lasting preference',forgotten:false}
  ]});fillThoughts($('thoughts'),items);api=async()=>({facts:[],used_tokens:0,budget_tokens:2000})`);
  const children = f.get('thoughts').children;
  assert.equal(children[0].tagName, 'DETAILS'); assert.equal(children[0].open, false);
  assert.equal(children[0].children[0].textContent, 'Used 1 memory');
  assert.equal(children[0].children[1].tagName, 'UL'); assert.equal(children[0].children[1].children[0].textContent, 'Deleted later');
  assert.equal(children[1].children[1].getAttribute('aria-label'), 'Forget this memory');
  assert.equal(children[1].children[0].textContent, 'Remembered: A lasting preference');
  assert.equal(children[1].children[1].textContent, '×');
  await children[1].children[1].events.click();
  assert.equal(f.get('thoughts').children[1].children[0].textContent, 'Forgotten: A lasting preference');
  assert.equal(f.get('thoughts').children[1].children.length, 1);
  const frame = f.run(`applyStreamFrame(null,{reset:true,text:'Reply',thoughts:'',persona_name:'Guide',memories:[{id:1,text:'Snapshot'}]},'chat')`);
  assert.equal(frame.persona_name, 'Guide'); assert.equal(frame.memories[0].text, 'Snapshot');
});

function summaryRows() {
  return `globalThis.rows=[
    {id:1,turn_id:'a',role:'user',ordinal:1,state:'completed',content:'one'},{id:2,turn_id:'a',role:'assistant',ordinal:1,state:'completed',content:'reply one'},
    {id:3,turn_id:'b',role:'user',ordinal:2,state:'completed',content:'two'},{id:4,turn_id:'b',role:'assistant',ordinal:2,state:'completed',content:'reply two'},
    {id:5,turn_id:'c',role:'user',ordinal:3,state:'completed',content:'three'},{id:6,turn_id:'c',role:'assistant',ordinal:3,state:'completed',content:'reply three'}]`;
}
const dividerIndexes = f => f.get('transcript').children.map((node, index) => node.className === 'summary-divider' ? index : -1).filter(index => index >= 0);
const afterMessage = (f, index) => f.get('transcript').children.slice(0, index).filter(node => node.className?.startsWith('message ')).length;

test('summary divider follows the last loaded message at or before its boundary and never repeats', async () => {
  const f = fixture(); await tick();
  f.run(`state.backend='native';state.current='chat';${summaryRows()};
    state.messageHistory={id:'chat',rows,loaded:false,loading:false,summaries:[{id:1,through_ordinal:2,text:'First summary',created_at:'2026-10-01T10:00:00Z'}]};renderMessages(rows)`);
  const [index] = dividerIndexes(f);
  assert.equal(dividerIndexes(f).length, 1);
  assert.equal(afterMessage(f, index), 4);
  const divider = f.get('transcript').children[index];
  assert.equal(divider.children[0].textContent, 'Earlier messages summarized');
  assert.equal(divider.children[1].textContent, 'Read summary');
  assert.equal(f.get('transcript').children[index - 1].className.startsWith('under'), true);
  f.run('renderMessages(rows)');
  assert.equal(dividerIndexes(f).length, 1);
});

test('summary dividers: several summaries, and none without a qualifying loaded message', async () => {
  const f = fixture(); await tick();
  f.run(`state.backend='native';state.current='chat';${summaryRows()};
    state.messageHistory={id:'chat',rows,loaded:false,loading:false,summaries:[
      {id:1,through_ordinal:1,text:'S1',created_at:'2026-10-01T10:00:00Z'},{id:2,through_ordinal:3,text:'S2',created_at:'2026-10-02T10:00:00Z'}]};renderMessages(rows)`);
  assert.deepEqual(dividerIndexes(f).map(index => afterMessage(f, index)), [2, 6]);
  // Older history not paged in: the loaded rows start after the boundary.
  f.run(`state.messageHistory.summaries=[{id:3,through_ordinal:0,text:'Old',created_at:'2026-10-01T10:00:00Z'}];renderMessages(rows)`);
  assert.equal(dividerIndexes(f).length, 0);
  f.run(`state.messageHistory.summaries=[];renderMessages(rows)`);
  assert.equal(dividerIndexes(f).length, 0);
});

test('summary snapshots from the transcript response and stream frames update dividers', async () => {
  const f = fixture(async () => ({ok: true, json: async () => ({})})); await tick();
  f.run(`state.backend='native';state.current='chat';${summaryRows()};state.messageHistory={id:'chat',rows,loaded:false,loading:false};
    api=async()=>({messages:rows,working:false,summaries:[{id:4,through_ordinal:1,text:'Saved',created_at:'2026-10-01T10:00:00Z'}]});`);
  await f.run(`loadMessages('chat')`);
  assert.equal(f.run('state.messageHistory.summaries[0].id'), 4);
  assert.deepEqual(dividerIndexes(f).map(index => afterMessage(f, index)), [2]);
  f.run(`globalThis.sources=[];globalThis.EventSource=function(){sources.push(this);this.close=()=>{};this.readyState=1};openStream('chat');loadMessages=async()=>{}`);
  f.run(`sources[0].onmessage({data:JSON.stringify({reset:true,turn_id:'t',started:1,summaries:[{id:4,through_ordinal:1,text:'Saved'},{id:9,through_ordinal:2,text:'Live'}]})})`);
  assert.equal(f.run('state.messageHistory.summaries.length'), 2);
  assert.deepEqual(dividerIndexes(f).map(index => afterMessage(f, index)), [2, 4]);
});

test('summary sheet is read-only, shows the text and written time, and closes', async () => {
  const f = fixture(); await tick();
  f.run(`state.backend='native';state.current='chat';${summaryRows()};
    state.messageHistory={id:'chat',rows,loaded:false,loading:false,summaries:[{id:1,through_ordinal:1,text:'Line one\\nLine two',created_at:'2026-10-01T10:00:00Z'}]};renderMessages(rows)`);
  const divider = f.get('transcript').children[dividerIndexes(f)[0]];
  divider.events.click();
  assert.equal(f.get('summary-sheet').open, true);
  assert.equal(f.get('summary-text').textContent, 'Line one\nLine two');
  assert.match(f.get('summary-written').textContent, /^Written .*Oct/);
  f.get('summary-close').events.click();
  assert.equal(f.get('summary-sheet').open, false);
});

test('summarize trace items use plain sentences for running, completed and error', async () => {
  const f = fixture(); await tick();
  const labels = ['running', 'completed', 'error'].map(status => {
    f.run(`state.backend='native';fillThoughts($('thoughts'),[{kind:'tool',id:'compaction-1',name:'summarize',arguments:'{}',status:'${status}',result:'ignored'}])`);
    const tool = f.get('thoughts').children[0];
    assert.equal(tool.children.length, 1);
    assert.equal(tool.children[0].className, 'tool-call tool-call-plain');
    return tool.children[0].textContent;
  });
  assert.deepEqual(labels, ['Summarizing earlier messages…', 'Summarized earlier messages', "Couldn't summarize earlier messages"]);
});

const fallbackServer = (extra = {}) => ({base_url: 'https://fallback.example.invalid/v1', has_key: true, default_model: 'backup',
  models: [{id: 'backup', display_name: 'Backup', enabled: true, thinking_mode: 'unsupported'}], ...extra});
const walkNodes = node => [node, ...(node.children || []).flatMap(walkNodes)];

test('Settings Models shows a fallback server section with its own fields, ids and model list', async () => {
  const f = fixture(); await tick();
  const nodes = () => walkNodes(f.get('settings-detail'));
  const headings = () => nodes().filter(node => node.tagName === 'H2').map(node => node.textContent);
  f.run(`state.backend='native';state.settings={use_fallback:false};state.settingsGroup='models';
    state.primary={base_url:'https://primary.example.invalid/v1',has_key:false,default_model:'one',models:[{id:'one',display_name:'First',enabled:true,thinking_mode:'unsupported'}]};
    state.fallback=${JSON.stringify(fallbackServer())};renderSettings()`);
  assert.deepEqual(headings().filter(text => /server|models|fallback/i.test(text)), ['Primary server', 'Models', 'Fallback server', 'Fallback models', 'Use the fallback']);
  const byId = id => nodes().find(node => node.id === id);
  assert.equal(byId('settings-base-url').value, 'https://primary.example.invalid/v1');
  assert.equal(byId('settings-fallback-base-url').value, 'https://fallback.example.invalid/v1');
  assert.equal(byId('settings-fallback-api-key').type, 'password');
  assert.equal(byId('settings-fallback-api-key').placeholder, 'Key saved');
  assert.deepEqual(byId('settings-fallback-default-model').children.map(node => node.value), ['backup']);
  assert.deepEqual(byId('settings-default-model').children.map(node => node.value), ['one']);
  // Expanding a fallback model leaves the primary's rows alone.
  byId('settings-fallback-model-0').events.click();
  assert.equal(f.run('state.openModel'), 'fallback:backup');
  assert.equal(byId('settings-fallback-model-0').getAttribute('aria-expanded'), 'true');
  assert.equal(byId('settings-model-0').getAttribute('aria-expanded'), 'false');
});

test('fallback server buttons save to the fallback routes, never the primary', async () => {
  const f = fixture(); await tick();
  const nodes = () => walkNodes(f.get('settings-detail'));
  f.run(`state.backend='native';state.settings={use_fallback:false};state.settingsGroup='models';globalThis.calls=[];
    api=async (path,options)=>{calls.push([path,options&&options.method]);return path.endsWith('/models')?{}:{base_url:'https://fallback.example.invalid/v1',has_key:true}};loadModels=async()=>{};
    state.primary={base_url:'https://primary.example.invalid/v1',has_key:false,default_model:null,models:[]};
    state.fallback=${JSON.stringify(fallbackServer())};renderSettings()`);
  const buttons = text => nodes().filter(node => node.tagName === 'BUTTON' && node.textContent === text);
  assert.equal(buttons('Save server').length, 2);
  await buttons('Save server')[1].events.click();
  await buttons('Save models')[1].events.click();
  assert.deepEqual(JSON.parse(f.run('JSON.stringify(calls)')), [['/settings/servers/fallback', 'PUT'], ['/settings/servers/fallback/models', 'PUT']]);
});

test('the fallback switch needs a saved fallback server and default model, and saves a boolean', async () => {
  const f = fixture(); await tick();
  const toggle = () => walkNodes(f.get('settings-detail')).find(node => node.id === 'settings-use-fallback');
  const hints = () => walkNodes(f.get('settings-detail')).filter(node => node.className === 'sheet-note').map(node => node.textContent);
  const show = (fallback, on = false) => f.run(`state.backend='native';state.settings={use_fallback:${on}};state.settingsGroup='models';
    state.primary={base_url:'',has_key:false,default_model:null,models:[]};state.fallback=${JSON.stringify(fallback)};renderSettings()`);
  show({base_url: '', has_key: false, default_model: null, models: []});
  assert.equal(toggle().disabled, true); assert.equal(toggle().getAttribute('aria-checked'), 'false'); assert.equal(toggle().className, 'job-toggle off');
  assert.match(hints().join(' '), /Save a fallback server and choose its default model first/);
  show(fallbackServer({default_model: null}));
  assert.equal(toggle().disabled, true);
  show(fallbackServer());
  assert.equal(toggle().disabled, false); assert.deepEqual(hints(), []);
  f.run(`globalThis.sent=[];saveSettings=async changes=>{sent.push(changes);return true}`);
  toggle().events.click();
  assert.deepEqual(JSON.parse(f.run('JSON.stringify(sent)')), [{use_fallback: true}]);
  show(fallbackServer({base_url: '', default_model: null}), true);
  assert.equal(toggle().disabled, false); assert.equal(toggle().getAttribute('aria-checked'), 'true'); assert.equal(toggle().className, 'job-toggle');
  toggle().events.click();
  assert.deepEqual(JSON.parse(f.run('JSON.stringify(sent)')).pop(), {use_fallback: false});
  const privacy = walkNodes(f.get('settings-detail')).map(node => node.textContent || '').join('\n');
  assert.match(privacy, /leave this server, if that server is hosted elsewhere/);
  assert.match(privacy, /Use the fallback when the primary can’t start a reply/);
});

test('Keep this chat local is a chat setting that loads and saves with the sheet', async () => {
  const f = fixture(); await tick();
  const html = fs.readFileSync(path.join(__dirname, '../roost/static/index.html'), 'utf8');
  assert.match(html, /id="sheet-keep-local" type="checkbox"> Keep this chat local/);
  assert.match(fs.readFileSync(path.join(__dirname, '../roost/static/style.css'), 'utf8'), /#sheet-instructions-form \.check-row \{ min-height: 44px/);
  f.run(`state.backend='native';state.sheetItem={id:'chat'};state.sheetBusy=false;globalThis.puts=[];
    loadModels=async()=>{};loadPersonas=async()=>{};fillModelPicker=()=>{};fillPersonaPicker=()=>{};updateSheetThinking=()=>{};
    api=async(path,options)=>{if(options&&options.method==='PUT'){puts.push(JSON.parse(options.body));return {instructions:'',model:null,reasoning:'none',no_web:false,keep_local:true}}
      return {instructions:'',model:null,reasoning:'none',no_web:false,keep_local:true,persona_id:null}}`);
  await f.run('openInstructions()');
  assert.equal(f.get('sheet-keep-local').checked, true);
  f.get('sheet-keep-local').checked = false;
  await f.run('saveInstructions({preventDefault(){}})');
  assert.equal(f.run('puts[0].keep_local'), false);
  f.get('sheet-keep-local').checked = true; f.get('sheet-instructions-text').disabled = false;
  await f.run('saveInstructions({preventDefault(){}})');
  assert.equal(f.run('puts[1].keep_local'), true);
});

test('fallback replies say which model answered; primary replies add nothing', async () => {
  const f = fixture(); await tick();
  const base = "roost_usage:{input_tokens:12,output_tokens:3,tokens_per_second:20,time_to_first_token:.5}";
  assert.equal(f.run(`replyUsage({turn_id:'t',${base}})`), '12 in / 3 out · 20.0 TPS · 0.50 s to first token');
  assert.equal(f.run(`replyUsage({turn_id:'t',${base},served_by:{server:'primary',model:'m',display_name:'Main'}})`), '12 in / 3 out · 20.0 TPS · 0.50 s to first token');
  assert.equal(f.run(`replyUsage({turn_id:'t',${base},served_by:{server:'fallback',model:'backup-model',display_name:'Backup'}})`),
    '12 in / 3 out · 20.0 TPS · 0.50 s to first token · via Backup · fallback');
  assert.match(f.run(`replyUsage({turn_id:'t',${base},served_by:{server:'fallback',model:'backup-model'}})`), /· via backup-model · fallback$/);
});

test('the fallback trace item is one plain sentence, running or settled', async () => {
  const f = fixture(); await tick();
  const sentence = "Primary couldn't start (HTTP 503); answered with Backup on the fallback server";
  f.run(`state.backend='native';fillThoughts($('thoughts'),[{kind:'tool',id:'fallback-1',name:'fallback',arguments:'{}',status:'completed',result:${JSON.stringify(sentence)}}])`);
  const tool = f.get('thoughts').children[0];
  assert.equal(tool.children.length, 1);
  assert.equal(tool.children[0].className, 'tool-call tool-call-plain');
  assert.equal(tool.children[0].textContent, sentence);
});

test('the fallback trace item shows its sentence while running and after an interruption', async () => {
  const f = fixture(); await tick();
  for (const [status, result] of [['running', "Primary couldn't start (HTTP 503); answering with Backup on the fallback server"],
                                  ['interrupted', "Primary couldn't start (HTTP 503); switched to Backup on the fallback server"],
                                  ['error', "Primary couldn't start (HTTP 503); Backup on the fallback server failed too"]]) {
    f.run(`state.backend='native';fillThoughts($('thoughts'),[{kind:'tool',id:'fallback-1',name:'fallback',arguments:'{}',status:${JSON.stringify(status)},result:${JSON.stringify(result)}}])`);
    const tool = f.get('thoughts').children[0];
    assert.equal(tool.children.length, 1);
    assert.equal(tool.children[0].textContent, result);
  }
});
