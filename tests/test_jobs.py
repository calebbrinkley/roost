"""Native jobs against a fake clock, SQLite and a loopback model; no Hermes."""

import asyncio
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from roost import schedules
from roost.jobs import Jobs, Outbox, enqueue, serve, deliver
from roost.store import Store, StoreError, MIGRATIONS
import test_native
from test_native import wait_for
from uuid import uuid4
from roost.servers import Servers


def instant(value):
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp()


class ScheduleTests(unittest.TestCase):
    def test_cron_day_wildcard_steps_and_restricted_or(self):
        for expression, after, expected in (
            ('0 8 */1 * 1', '2026-10-29', '2026-11-02T08:00'),
            ('0 8 15 * */1', '2026-10-29', '2026-11-15T08:00'),
            ('0 8 1,15 * 1', '2026-10-29', '2026-11-01T08:00'),
            ('0 8 1,15 * 1', '2026-11-01T09:00', '2026-11-02T08:00'),
            ('0 8 * * 1-5', '2026-10-30T09:00', '2026-11-02T08:00'),
        ):
            with self.subTest(expression=expression, after=after):
                schedule = {'kind': 'cron', 'expr': expression}
                due = schedules.occurrence(schedule, 'UTC', 0, instant(after))
                self.assertEqual(due, instant(expected))
                self.assertEqual(schedules.occurrence(schedule, 'UTC', 0, due, previous=True), due)

    def test_cron_fields_ranges_steps_lists_and_sunday(self):
        parsed = schedules.cron('*/15 8-10 1,15 * 0,7')
        self.assertEqual(parsed[0], {0, 15, 30, 45})
        self.assertEqual(parsed[1], {8, 9, 10})
        self.assertEqual(parsed[4], {0})
        for expression in ('', '* * * *', '60 * * * *', '0 24 * * *', '0 0 0 * *', '* * * 13 *', '* * * * 8', '*/0 * * * *', '3-1 * * * *', 'a * * * *'):
            with self.subTest(expression=expression), self.assertRaises(StoreError):
                schedules.cron(expression)
        schedule = {'kind': 'cron', 'expr': '0 8 15 * 1'}
        # Restricted day-of-month and weekday use standard cron OR semantics.
        self.assertEqual(schedules.occurrence(schedule, 'UTC', 0, instant('2026-10-29')), instant('2026-11-02T08:00'))

    def test_dst_gap_and_overlap_once_only(self):
        schedule = {'kind': 'cron', 'expr': '30 2 * * *'}
        due = schedules.occurrence(schedule, 'America/New_York', 0, instant('2026-03-08T00:00'))
        self.assertEqual(due, instant('2026-03-08T07:00'))
        self.assertEqual(schedules.occurrence(schedule, 'America/New_York', 0, due), instant('2026-03-09T06:30'))
        schedule['expr'] = '30 1 * * *'
        due = schedules.occurrence(schedule, 'America/New_York', 0, instant('2026-11-01T00:00'))
        self.assertEqual(due, instant('2026-11-01T05:30'))
        self.assertEqual(schedules.occurrence(schedule, 'America/New_York', 0, due), instant('2026-11-02T06:30'))
        self.assertEqual(schedules.occurrence(schedule, 'America/New_York', 0, instant('2026-11-01T06:40'), previous=True), due)

    def test_every_and_once_local_time(self):
        start = instant('2026-03-07T13:00')
        every = {'kind': 'every', 'value': 1, 'unit': 'days'}
        self.assertEqual(schedules.occurrence(every, 'America/New_York', start, start), instant('2026-03-08T12:00'))
        for unit, seconds in (('minutes', 60), ('hours', 3600)):
            every.update(unit=unit, value=2)
            self.assertEqual(schedules.occurrence(every, 'UTC', start, start), start + 2 * seconds)
        once = {'kind': 'once', 'at': '2026-03-08T02:30'}
        due = schedules.occurrence(once, 'America/New_York', 0, start)
        self.assertEqual(due, instant('2026-03-08T07:00'))
        self.assertIsNone(schedules.occurrence(once, 'America/New_York', 0, due))
        for bad in ({'kind': 'every', 'value': True, 'unit': 'hours'}, {'kind': 'once', 'at': '2026-01-01T12:00Z'}, {'kind': 'cron', 'expr': '* * * * *', 'extra': 1}):
            with self.assertRaises(StoreError):
                schedules.validate(bad)

    def test_migration_preserves_existing_data_and_refuses_populated_downgrade(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'chat.sqlite3')
            store.migrate(len(MIGRATIONS) - 1)
            cid = store.create_conversation('Existing').id
            store.save_memory('Existing fact')
            store.migrate()
            self.assertEqual(store.get_conversation(cid).title, 'Existing')
            self.assertIsNone(store.memories()[0]['job_id'])
            store.migrate(len(MIGRATIONS) - 1)
            store.migrate()
            enqueue(store, 'test', {'title': 'Example'}, 0)
            with self.assertRaises(StoreError):
                store.migrate(len(MIGRATIONS) - 1)
            self.assertEqual(store.db.execute('SELECT count(*) FROM push_outbox').fetchone()[0], 1)
            store.close()


class JobTests(unittest.IsolatedAsyncioTestCase):
    request = test_native.NativeTests.request
    settled = test_native.NativeTests.settled

    async def asyncSetUp(self):
        await test_native.NativeTests.asyncSetUp(self)
        for key in ('job_scheduler', 'outbox_worker'):
            self.app[key].cancel()
            await asyncio.gather(self.app[key], return_exceptions=True)
        self.now = instant('2026-10-29T12:00')
        self.jobs = self.app['jobs']
        self.jobs.clock = lambda: self.now
        self.fake.delay = 0

    async def asyncTearDown(self):
        await test_native.NativeTests.asyncTearDown(self)

    async def job(self, **changes):
        return await self.request('POST', '/api/jobs', {'name': 'Daily report', 'prompt': 'Report on the garden',
            'schedule': {'kind': 'every', 'value': 1, 'unit': 'hours'}, **changes}, 201)

    async def complete(self, jid):
        row = self.jobs.runs(jid)[0]
        await self.settled(row['turn_id'])
        await self.jobs.tick()
        await asyncio.gather(*list(self.jobs.finishing.values()))
        return self.jobs.runs(jid)[0]

    async def test_due_unique_claim_catchup_and_no_overlap(self):
        job = await self.job()
        self.now += 3600 * 9 + 60
        self.fake.delay = .1
        await self.jobs.tick()
        await self.jobs.tick()
        runs = self.jobs.runs(job['id'])
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]['scheduled_for'], job['created_at'] + 9 * 3600)
        self.assertIsNone(self.jobs.claim(self.jobs.get(job['id']), runs[0]['scheduled_for']))
        self.now += 3600
        await self.jobs.tick()
        self.assertEqual([r['state'] for r in self.jobs.runs(job['id'])], ['skipped', 'running'])
        await self.settled(runs[0]['turn_id'])
        await self.jobs.tick()
        await asyncio.gather(*list(self.jobs.finishing.values()))
        self.assertEqual(self.jobs.runs(job['id'])[1]['state'], 'completed')

    async def test_trigger_pause_resume_once_and_delete_all(self):
        job = await self.job()
        base = '/api/jobs/' + job['id']
        self.assertFalse((await self.request('POST', base + '/pause'))['enabled'])
        self.now += 7200
        await self.jobs.tick()
        self.assertEqual(self.jobs.runs(job['id']), [])
        resumed = await self.request('POST', base + '/resume')
        self.assertTrue(resumed['enabled'])
        self.assertGreater(resumed['next_due'], self.now)
        await self.request('POST', base + '/trigger')
        run = await self.complete(job['id'])
        self.assertEqual(run['state'], 'completed')
        self.assertEqual(len((await self.request('GET', base + '/runs'))['runs']), 1)
        once = await self.job(schedule={'kind': 'once', 'at': '2026-10-29T14:01'}, time_zone='UTC')
        self.now += 60
        await self.jobs.tick()
        self.assertFalse(self.jobs.get(once['id'])['enabled'])
        self.assertEqual((await self.complete(once['id']))['state'], 'completed')
        self.assertEqual(await self.request('DELETE', '/api/jobs', {'confirm': 'DELETE'}), {'deleted': 2})
        self.assertEqual(self.jobs.listing(), [])

    async def test_settled_turn_allows_next_claim_before_completion_processing(self):
        job = await self.job()
        await self.request('POST', '/api/jobs/' + job['id'] + '/trigger')
        first = self.jobs.runs(job['id'])[0]
        await self.settled(first['turn_id'])
        self.assertEqual(self.jobs.runs(job['id'])[0]['state'], 'completed')
        self.now = job['next_due']
        await self.jobs.tick()
        self.assertEqual([r['state'] for r in self.jobs.runs(job['id'])], ['running', 'completed'])
        await asyncio.gather(*list(self.jobs.finishing.values()))
        self.assertIsNotNone(self.store.db.execute('SELECT 1 FROM push_outbox WHERE id=?', ('job-' + first['run_id'],)).fetchone())

    async def test_pending_notifications_recover_in_run_order_after_restart(self):
        job = await self.job(notify='changed')
        self.fake.completion['choices'][0]['message']['content'] = 'SAME'
        for _ in range(2):
            self.now += 1
            await self.request('POST', '/api/jobs/' + job['id'] + '/trigger')
            await self.settled(self.jobs.runs(job['id'])[0]['turn_id'])
        self.assertEqual([r['state'] for r in self.jobs.runs(job['id'])], ['completed', 'completed'])
        self.assertIsNone(self.store.db.execute('SELECT 1 FROM push_outbox').fetchone())
        restarted = Jobs(self.store, self.app['runner'], clock=lambda: self.now)
        await restarted.tick()
        await asyncio.gather(*list(restarted.finishing.values()))
        self.assertEqual([r['changed'] for r in restarted.runs(job['id'])], [False, True])
        rows = self.store.db.execute('SELECT payload FROM push_outbox').fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0]['payload'])['body'], 'Hello')

    async def test_retry_waiting_for_admission_expires_and_frees_next_occurrence(self):
        job = await self.job()
        self.fake.status = 503
        await self.request('POST', '/api/jobs/' + job['id'] + '/trigger')
        run = self.jobs.runs(job['id'])[0]
        await self.settled(run['turn_id'])
        await self.jobs.tick()
        async with self.app['runner'].lock:
            self.now += 60
            await self.jobs.tick()
            self.assertEqual(self.store.get_turn(run['turn_id']).state, 'queued')
            self.now = run['deadline']
            await self.jobs.tick()
            self.assertEqual(self.store.get_turn(run['turn_id']).state, 'cancelled')
            self.assertEqual([r['state'] for r in self.jobs.runs(job['id'])], ['running', 'failed'])

    async def test_schedule_edit_recomputes_retry_deadline(self):
        for waiting in (False, True):
            with self.subTest(waiting=waiting):
                job = await self.job()
                self.fake.status = 503
                await self.request('POST', '/api/jobs/' + job['id'] + '/trigger')
                run = self.jobs.runs(job['id'])[0]
                await self.settled(run['turn_id'])
                await self.jobs.tick()
                async with self.app['runner'].lock:
                    if waiting:
                        self.now += 60
                        await self.jobs.tick()
                    edited = self.jobs.save({'schedule': {'kind': 'every', 'value': 2, 'unit': 'minutes'}}, job['id'])
                    row = self.jobs.runs(job['id'])[0]
                    self.assertEqual(row['deadline'], edited['next_due'])
                    self.now = edited['next_due']
                    await self.jobs.tick()
                    self.assertEqual(self.jobs.runs(job['id'])[1]['state'], 'failed')
                    self.assertNotIn(self.store.get_turn(run['turn_id']).state, ('queued', 'running'))
                await self.jobs.delete(job['id'])

    async def test_delete_cancels_running_turn_and_trigger_does_not_overlap(self):
        job = await self.job()
        self.fake.delay = .2
        path = '/api/jobs/' + job['id']
        await self.request('POST', path + '/trigger')
        run = self.jobs.runs(job['id'])[0]
        await wait_for(lambda: self.store.get_turn(run['turn_id']).state == 'running')
        self.now += 1
        await self.request('POST', path + '/trigger')
        self.assertEqual(self.jobs.runs(job['id'])[0]['state'], 'skipped')
        await self.request('DELETE', path, {'confirm': True})
        self.assertEqual(self.store.get_turn(run['turn_id']).state, 'interrupted')
        await self.request('GET', path, status=404)
        self.assertFalse(self.store.db.execute('SELECT 1 FROM push_outbox').fetchone())

    async def test_delete_schedule_does_not_cancel_reply_to_completed_run(self):
        job = await self.job()
        path = '/api/jobs/' + job['id']
        await self.request('POST', path + '/trigger')
        run = await self.complete(job['id'])
        self.fake.delay = .1
        reply = await self.request('POST', '/api/conversations/' + run['conversation_id'] + '/messages',
                                   {'text': 'Tell me more', 'request_id': str(uuid4())})
        await wait_for(lambda: self.store.get_turn(reply['turn_id']).state == 'running')
        await self.request('DELETE', path, {'confirm': True})
        await self.settled(reply['turn_id'])
        self.assertEqual(self.store.get_turn(reply['turn_id']).state, 'completed')

    async def test_retries_stay_primary_and_stop_before_next_occurrence(self):
        servers = Servers(self.state, environ={})
        servers.update('fallback', {'base_url': self.fake.url})
        servers.set_models('fallback', {'models': [{'id': 'example-model', 'display_name': 'Backup', 'enabled': True, 'thinking_mode': 'unsupported'}], 'default_model': 'example-model'})
        await self.request('PATCH', '/api/settings', {'use_fallback': True})
        job = await self.job()
        self.fake.status = 503
        await self.request('POST', '/api/jobs/' + job['id'] + '/trigger')
        row = self.jobs.runs(job['id'])[0]
        await self.settled(row['turn_id'])
        self.assertTrue(self.store.get_turn(row['turn_id']).stats['retryable_start'])
        await self.jobs.tick()
        self.assertEqual(self.jobs.runs(job['id'])[0]['state'], 'retrying')
        self.assertEqual(self.jobs.runs(job['id'])[0]['next_retry'], self.now + 60)
        self.now += 60
        self.fake.status = 200
        await self.jobs.tick()
        result = await self.complete(job['id'])
        self.assertEqual(result['state'], 'completed')
        self.assertFalse(self.store.fallback_step(row['turn_id']))
        # A one-minute schedule has no room for even the first retry.
        short = await self.job(schedule={'kind': 'every', 'value': 1, 'unit': 'minutes'})
        self.fake.status = 503
        await self.request('POST', '/api/jobs/' + short['id'] + '/trigger')
        run = await self.complete(short['id'])
        self.assertEqual(run['state'], 'failed')
        self.app['hermes'].start.assert_not_called()

    async def test_retry_backoff_survives_scheduler_restart_and_exhausts(self):
        job = await self.job()
        self.fake.status = 503
        await self.request('POST', '/api/jobs/' + job['id'] + '/trigger')
        run = self.jobs.runs(job['id'])[0]
        for attempt, delay in enumerate((60, 300, 900), 1):
            await self.settled(run['turn_id'])
            await self.jobs.tick()
            row = self.jobs.runs(job['id'])[0]
            self.assertEqual((row['state'], row['attempts'], row['next_retry']), ('retrying', attempt, self.now + delay))
            # Scheduler objects have no in-memory retry queue to lose at restart.
            restarted = Jobs(self.store, self.app['runner'], clock=lambda: self.now)
            self.now += delay
            await restarted.tick()
        await self.settled(run['turn_id'])
        await self.jobs.tick()
        self.assertEqual(self.jobs.runs(job['id'])[0]['state'], 'failed')
        self.assertFalse(self.store.fallback_step(run['turn_id']))

    async def test_snapshot_unavailable_retries_and_deleted_persona_uses_default(self):
        persona = self.store.save_persona('Gardener', 'Discuss plants.')
        job = await self.job(persona=persona['id'])
        self.store.delete_persona(persona['id'])
        self.assertIsNone(self.jobs.get(job['id'])['persona'])
        selected = self.app['runner'].snapshot
        self.app['runner'].snapshot = lambda *args, **kwargs: (_ for _ in ()).throw(StoreError('Primary is unavailable.'))
        await self.request('POST', '/api/jobs/' + job['id'] + '/trigger')
        self.assertEqual(self.jobs.runs(job['id'])[0]['state'], 'retrying')
        self.app['runner'].snapshot = selected
        self.now += 60
        await self.jobs.tick()
        self.assertEqual((await self.complete(job['id']))['state'], 'completed')

    async def test_unoffered_memory_is_refused_even_if_model_calls_it(self):
        job = await self.job(web=False)
        self.fake.scenario = 'remember'
        await self.request('POST', '/api/jobs/' + job['id'] + '/trigger')
        run = await self.complete(job['id'])
        self.assertEqual(run['state'], 'failed')
        self.assertEqual(self.store.memories(), [])

    async def test_memory_gating_and_tagging_and_overrides(self):
        persona = self.store.save_persona('Gardener', 'Discuss plants.')
        for allowed in (False, True):
            job = await self.job(allow_memory=allowed, web=False, persona=persona['id'], model='example-model')
            self.fake.scenario = 'remember' if allowed else None
            await self.request('POST', '/api/jobs/' + job['id'] + '/trigger')
            run = await self.complete(job['id'])
            turn = self.store.get_turn(run['turn_id'])
            self.assertEqual(turn.persona['name'], 'Gardener')
            self.assertEqual(turn.settings['remember_offered'], allowed)
            self.assertFalse(turn.settings['tools_offered'])
            sent = [r[2] for r in self.fake.requests if r[2] and r[2]['stream']][-1]
            if not allowed:
                self.assertNotIn('remember', json.dumps(sent.get('tools')))
                self.assertEqual(self.store.memories(), [])
            else:
                fact = self.store.memories()[0]
                self.assertEqual((fact['job_id'], fact['job_name']), (job['id'], job['name']))

    async def test_notify_changed_first_same_changed_and_durable_payload(self):
        job = await self.job(notify='changed')
        for number, answer, changed in ((0, 'SAME', True), (1, 'SAME', False), (2, 'CHANGED', True)):
            self.now += 1
            self.fake.completion['choices'][0]['message']['content'] = answer
            await self.request('POST', '/api/jobs/' + job['id'] + '/trigger')
            run = await self.complete(job['id'])
            self.assertEqual(run['changed'], changed)
            entries = self.store.db.execute('SELECT * FROM push_outbox').fetchall()
            self.assertEqual(len(entries), (1, 1, 2)[number])
        payload = json.loads(entries[-1]['payload'])
        self.assertEqual(payload['title'], job['name'])
        self.assertEqual(payload['body'], 'Hello')
        self.assertEqual(payload['url'], '/?c=' + run['conversation_id'])
        comparisons = [r[2] for r in self.fake.requests if r[2] and not r[2]['stream']]
        self.assertEqual(len(comparisons), 2)
        self.assertTrue(all('tools' not in r and r['chat_template_kwargs']['enable_thinking'] is False for r in comparisons))

    async def test_run_hidden_searchable_and_reply_is_normal_chat(self):
        job = await self.job()
        await self.request('POST', '/api/jobs/' + job['id'] + '/trigger')
        run = await self.complete(job['id'])
        self.assertEqual(await self.request('GET', '/api/conversations'), {'sessions': [], 'total': 0})
        found = await self.request('GET', '/api/search?q=garden')
        self.assertEqual(found['sessions'][0]['id'], run['conversation_id'])
        cid = run['conversation_id']
        transcript = await self.request('GET', f'/api/conversations/{cid}')
        self.assertEqual(transcript['job'], {'id': job['id'], 'name': job['name'], 'scheduled_for': run['scheduled_for']})
        accepted = await self.request('POST', f'/api/conversations/{cid}/messages', {'text': 'More about the garden', 'request_id': str(uuid4())})
        await self.settled(accepted['turn_id'])
        self.assertFalse(self.store.get_turn(accepted['turn_id']).settings.get('job_run', False))
        self.assertEqual(len((await self.request('GET', f'/api/conversations/{cid}'))['messages']), 4)

    async def test_delete_all_conversations_preserves_job_history_and_memory_label(self):
        job = await self.job(allow_memory=True)
        self.fake.scenario = 'remember'
        await self.request('POST', '/api/jobs/' + job['id'] + '/trigger')
        run = await self.complete(job['id'])
        await self.request('DELETE', '/api/conversations', {'confirm': 'DELETE'})
        self.assertIsNotNone(self.store.get_conversation(run['conversation_id']))
        self.assertEqual(self.jobs.runs(job['id'])[0]['conversation_id'], run['conversation_id'])
        await self.request('DELETE', '/api/jobs/' + job['id'], {'confirm': True})
        self.assertEqual(self.store.memories()[0]['job_name'], job['name'])
        with self.assertRaises(StoreError):
            self.store.migrate(len(MIGRATIONS) - 1)

    async def test_boundary_validation_and_owner_guards(self):
        good = {'name': 'Example', 'prompt': 'Report', 'schedule': {'kind': 'cron', 'expr': '0 8 * * *'}}
        for patch in ({'deliver': 'local'}, {'enabled': 1}, {'allow_memory': 'yes'}, {'web': None}, {'notify': 'never'}, {'time_zone': 'INVALID'}, {'model': 'missing'}, {'persona': 'missing'}, {'schedule': '* * * * *'}, {'name': ''}, {'prompt': ''}):
            await self.request('POST', '/api/jobs', {**good, **patch}, 400)
        job = await self.job()
        await self.request('POST', '/api/jobs/' + job['id'] + '/unknown', status=404)
        async with self.client.post('/api/jobs', json=good) as response:
            self.assertEqual(response.status, 403)
        self.client.session.cookie_jar.clear()
        for method, path, body in (('GET', '/api/jobs', None), ('POST', '/api/jobs', good), ('POST', '/api/jobs/' + job['id'] + '/trigger', None)):
            await self.request(method, path, body, 401)


class OutboxTests(unittest.IsolatedAsyncioTestCase):
    async def test_workers_survive_one_database_error_and_only_stop_on_cancellation(self):
        for loop, key in ((serve, 'jobs'), (deliver, 'outbox')):
            with self.subTest(worker=key):
                worker = SimpleNamespace(tick=AsyncMock(side_effect=[sqlite3.OperationalError('private database detail'), None, asyncio.CancelledError()]))
                with patch('roost.native.enabled', return_value=True), patch('roost.jobs.asyncio.sleep', new_callable=AsyncMock) as sleep:
                    with self.assertLogs('roost.jobs', level='WARNING') as logs:
                        with self.assertRaises(asyncio.CancelledError):
                            await loop({key: worker})
                    self.assertNotIn('private database detail', '\n'.join(logs.output))
                    self.assertEqual(worker.tick.await_count, 3)
                    self.assertEqual(sleep.await_count, 2)

    async def test_partial_retry_after_restart_and_stable_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'native.sqlite3'
            store = Store(path)
            now = [1000]
            push = SimpleNamespace(list=lambda: [{'id': 'one'}, {'id': 'two'}], send=AsyncMock(side_effect=[1, 0]))
            enqueue(store, 'stable', {'title': 'Example', 'tag': 'stable'}, now[0])
            enqueue(store, 'stable', {'title': 'Duplicate'}, now[0])
            worker = Outbox(store, push, None, clock=lambda: now[0])
            await worker.tick()
            row = store.db.execute('SELECT * FROM push_outbox').fetchone()
            self.assertEqual(json.loads(row['pending']), ['two'])
            self.assertEqual((row['attempts'], row['next_retry'], row['sent_at']), (1, 1060, None))
            store.close()
            store = Store(path)
            push.send = AsyncMock(return_value=1)
            worker = Outbox(store, push, None, clock=lambda: now[0])
            await worker.tick()
            push.send.assert_not_called()
            now[0] = 1060
            await worker.tick()
            self.assertEqual(push.send.call_args.kwargs, {'only': 'two'})
            self.assertEqual(push.send.call_args.args[1]['tag'], 'stable')
            self.assertEqual(store.db.execute('SELECT sent_at FROM push_outbox').fetchone()[0], 1060)
            store.close()
