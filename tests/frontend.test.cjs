const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function fixture() {
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
  const document = { getElementById: get, createElement: node, createElementNS: node, createTextNode: text => ({textContent: text}),
    querySelectorAll: () => [], querySelector: () => get('main'), addEventListener() {} };
  const context = vm.createContext({ document, console, setTimeout: () => 1, clearTimeout() {}, setInterval() {}, navigator: {},
    window: { roostTheme: { current: () => 'dark' }, addEventListener() {}, matchMedia: () => ({matches: false}), getSelection: () => '' },
    fetch: async () => ({ok: true, json: async () => ({signed_in: false})}), Date, Math });
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
    state.pending = {id:'one'}; state.jobRuns = {job: [{preview:'private report'}]}; state.search = 'old';`);
  f.get('job-dialog').open = true; f.get('conversation-sheet').open = true; f.get('message').value = 'private draft';
  f.get('transcript').append({textContent:'private transcript'});
  f.run('signedOut()');
  assert.equal(f.run('state.current'), null); assert.equal(f.run('state.pending'), null);
  assert.equal(f.run('state.conversations.length'), 0); assert.equal(f.run('state.jobs.length'), 0);
  assert.equal(f.get('message').value, ''); assert.equal(f.get('transcript').children.length, 0);
  assert.equal(f.get('job-dialog').open, false); assert.equal(f.get('conversation-sheet').open, false);
});

test('older transcript response cannot overwrite a newer poll', async () => {
  const f = fixture(); await tick();
  f.run(`state.current = 'one'; globalThis.requests = []; globalThis.rendered = '';
    api = () => new Promise(resolve => requests.push(resolve)); renderMessages = rows => { rendered = rows[0].content; };`);
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
    api = async () => [{id:'one'}]; renderJobs = () => {}; loadJobRuns = async () => { historyLoads++; };`);
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

test('a response from before sign-out cannot repopulate conversations', async () => {
  const f = fixture(); await tick();
  f.run(`api = () => new Promise(resolve => { globalThis.finish = resolve; });`);
  const loading = f.run('loadConversations()');
  f.run(`signedOut(); finish({sessions:[{id:'old',title:'private'}],total:1});`);
  await loading; assert.equal(f.run('state.conversations.length'),0);
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
  assert.equal(f.run(`scheduleLabel('0 8 * * 1-5')`),'Weekdays at 08:00');
});

test('refreshing loaded pages after insertion leaves no gap and Load more advances correctly', async () => {
  const f = fixture(); await tick();
  f.run(`state.current = 'd'; state.conversations = ['a','b','c','d'].map(id => ({id}));
    api = async path => { const offset = Number(path.split('offset=')[1]);
      return {sessions:['new','a','b','c','d'].slice(offset,offset+2).map(id => ({id})), total:5}; };
    renderConversations = () => {};`);
  await f.run('loadConversations()');
  assert.deepEqual(Array.from(f.run('state.conversations.map(conversationId)')),['new','a','b','c']);
  await f.run('loadConversations(true)');
  assert.deepEqual(Array.from(f.run('state.conversations.map(conversationId)')),['new','a','b','c','d']);
});

test('saving a Job re-enables its Run now and Edit controls immediately', async () => {
  const f = fixture(); await tick();
  f.run(`api = async path => path === '/jobs' ? [{id:'one'}] : {id:'one',runs:[]};`);
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
  f.run(`api = async path => path === '/jobs' ? [null,{id:'one',name:'valid job'}] : {runs:[null,{preview:'valid run'}]};`);
  await f.run('loadJobs()');
  assert.match(text(f.get('sidebar-jobs-list')),/valid job/);
  assert.match(text(f.get('job-detail')),/valid run/);
});

test('transcript keeps updating when every poll overlaps the next one', async () => {
  const f = fixture(); await tick();
  f.run(`state.current = 'one'; globalThis.requests = []; globalThis.rendered = [];
    api = () => new Promise(resolve => requests.push(resolve)); renderMessages = rows => { rendered.push(rows[0].content); };`);
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
    api = () => new Promise(resolve => requests.push(resolve)); renderMessages = rows => { rendered.push(rows[0].content); };`);
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
  assert.deepEqual(Array.from(f.run('state.conversations.map(conversationId)')), ['b','c','d']);
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
  assert.deepEqual(Array.from(f.run('state.conversations.map(conversationId)')), ['b']);
  assert.equal(f.run('state.conversationOffset'), 1);
});

test('a created Job appears and stays even when the next list read does not have it yet', async () => {
  const f = fixture(); await tick();
  f.run(`api = async (path, options = {}) => {
      if (options.method === 'POST') return {id:'new', name:'Fresh', enabled:true};
      if (path === '/jobs') return [];
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
      if (path === '/jobs') return [{id:'one', enabled:true}];
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

function settingsFixture() {
  const f = fixture();
  Object.assign(f.context.window.roostTheme, { mode: 'system', accent: 'forest',
    choice() { return this.mode; }, set(mode) { this.mode = mode; }, setAccent(name) { this.accent = name; } });
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

test('saving an agent name applies the name the server now reports', async () => {
  const f = settingsFixture(); await tick();
  f.run(`api = async (path, options = {}) => {
    if (path === '/me') return {signed_in: true, agent: 'Robin', accent: 'forest'};
    if (options.method === 'PATCH') return {...${SETTINGS}, agent_name: 'Robin'};
    if (path === '/settings/soul') return {content: 'Be brief.'};
    return ${SETTINGS}; };`);
  f.run(`setSection('settings'); selectSettingsGroup('agent')`); await tick(); await tick();
  const input = find(f.get('settings-detail'), n => n.id === 'settings-agent-name');
  assert.equal(input.placeholder, 'Kestrel');
  assert.equal(find(f.get('settings-detail'), n => n.id === 'settings-soul').value, 'Be brief.');
  input.value = 'Robin'; input.events.input();
  await find(f.get('settings-detail'), n => n.textContent === 'Save name').events.click(); await tick();
  assert.equal(f.run('agent()'), 'Robin');
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

test('the gear opens Settings and the section tabs stay for Messages and Jobs', async () => {
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
             state_bytes:24576, disk_total_bytes:1e12, disk_free_bytes:4e11},
      hermes:{reachable:true, latency_ms:12, version:'2026.9.24', gateway_running:true, active_sessions:2}};
    api = async path => path === '/about' ? about : ${SETTINGS};`);
  f.run(`setSection('settings'); selectSettingsGroup('about')`); await tick(); await tick();
  const shown = text(f.get('settings-detail'));
  for (const part of [/0\.1\.0/, /e2ebc26/, /2h 1m/, /50 MB/, /Running/, /12 ms/, /Space Grotesk/]) assert.match(shown, part);
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
