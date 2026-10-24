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
      value: '', textContent: '', disabled: false, open: false, dataset: {}, style: {}, children: [], events: {},
      scrollTop: 0, scrollHeight: 100, clientHeight: 100,
      classList: { add(...names) { names.forEach(n => classes.add(n)); }, remove(...names) { names.forEach(n => classes.delete(n)); },
        contains(n) { return classes.has(n); }, toggle(n, on = !classes.has(n)) { on ? classes.add(n) : classes.delete(n); } },
      append(...children) { this.children.push(...children); }, replaceChildren(...children) { this.children = children; },
      remove() {}, setAttribute() {}, addEventListener(type, fn) { this.events[type] = fn; }, focus() {}, close() { this.open = false; },
      showModal() { this.open = true; }, querySelectorAll() { return []; }, closest() { return null; },
    };
  }
  const get = id => { if (!nodes.has(id)) nodes.set(id, node()); return nodes.get(id); };
  get('app').classList.add('hidden');
  const document = { getElementById: get, createElement: tag => Object.assign(node(), {tagName: String(tag).toUpperCase()}), createElementNS: node, createTextNode: text => ({textContent: text}),
    querySelectorAll: () => [], querySelector: () => get('main'), events: {}, addEventListener(name, fn) { this.events[name] = fn; } };
  const context = vm.createContext({ document, console, setTimeout: () => 1, clearTimeout() {}, setInterval() {}, navigator: {},
    window: { roostTheme: { current: () => 'dark' }, addEventListener() {}, matchMedia: () => ({matches: false}), getSelection: () => '' },
    fetch, AbortSignal, URLSearchParams, Date, Math, atob, btoa, ...globals });
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

test('delete all stays disabled until DELETE is typed, then sends that confirmation', async () => {
  const f = settingsFixture(); await tick();
  f.run(`globalThis.calls = []; api = async (path, options = {}) => {
    if (options.method === 'DELETE') { calls.push([path, JSON.parse(options.body)]); return {deleted: 3}; }
    return ${SETTINGS}; };
    state.conversations = [{id:'a'}]; state.current = 'a'; renderConversations = () => {}; renderWelcome = () => {};`);
  f.run(`setSection('settings'); selectSettingsGroup('data')`); await tick();
  const input = find(f.get('settings-detail'), n => n.id === 'confirm-conversations');
  const button = find(f.get('settings-detail'), n => n.textContent === 'Delete all conversations' && n.className === 'danger-button');
  assert.equal(button.disabled, true);
  input.value = 'delete'; input.events.input();
  assert.equal(button.disabled, true);
  input.value = 'DELETE'; input.events.input();
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
  assert.equal(f.get('message').value, 'Dictated draft');
  assert.equal(f.run('state.voiceDraft'), true);
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
  assert.deepEqual(JSON.parse(f.run('calls[0][2]')), {text: 'hello', reasoning: 'none'});
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
  f.run(`state.current = 'one'; state.lastRows = []; state.live = {id:'one', text:'First chunk'};
    state.liveNodes = {shownText:'First chunk'}; fillLive = live => {state.liveNodes.shownText = live.text;};`);
  const transcript = f.get('transcript'), jump = f.get('jump-latest');
  transcript.scrollHeight = 1000; transcript.clientHeight = 200; transcript.scrollTop = 100;
  f.run('renderLive()'); assert.equal(jump.classList.contains('unread'), false);
  f.run(`state.live.text += ' next chunk'; renderLive();`);
  assert.equal(transcript.scrollTop, 100); assert.equal(jump.classList.contains('unread'), true);
});
