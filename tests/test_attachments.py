"""Attachments: extraction, the extract route, send validation, prompt layout, compaction, edit and migration."""

import asyncio
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from aiohttp import ClientSession, FormData
from pypdf import PdfWriter

from roost import attachments
from roost.attachments import AttachmentError
from roost.store import Store, StoreError
import test_chat
from test_chat import without_clock



def pdf_with_text(*pages):
    """A tiny valid PDF with one line of text per page, built by hand so no fixture file is needed."""
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>', b'', b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>']
    kids = []
    for text in pages:
        page, stream = 4 + 2 * len(kids), 5 + 2 * len(kids)
        kids.append(f'{page} 0 R')
        body = f'BT /F1 12 Tf 20 100 Td ({text}) Tj ET'.encode()
        objects.append(f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Contents {stream} 0 R '
                       f'/Resources << /Font << /F1 3 0 R >> >> >>'.encode())
        objects.append(b'<< /Length %d >>\nstream\n%s\nendstream' % (len(body), body))
    objects[1] = f'<< /Type /Pages /Kids [{" ".join(kids)}] /Count {len(kids)} >>'.encode()
    out, offsets = bytearray(b'%PDF-1.4\n'), []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b'%d 0 obj\n%s\nendobj\n' % (number, body)
    xref = len(out)
    out += b'xref\n0 %d\n0000000000 65535 f \n' % (len(objects) + 1)
    out += b''.join(b'%010d 00000 n \n' % offset for offset in offsets)
    out += b'trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n' % (len(objects) + 1, xref)
    return bytes(out)


def blank_pdf(pages=1, password=None):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    if password:
        writer.encrypt(password)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def sample(name='notes.txt', text='Synthetic notes about gardening.'):
    return {'name': name, 'text': text}


PNG = b'\x89PNG\r\n\x1a\n\0\0\0\rIHDR' + b'\0' * 32


class ExtractionTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_and_source_files(self):
        for name in ('a.txt', 'a.md', 'DATA.CSV', 'x.json', 'main.py', 'Dockerfile'):
            result = await attachments.extract(name, 'héllo\nworld'.encode())
            self.assertEqual((result['kind'], result['chars'], result['tokens'], result['text']), ('text', 11, 3, 'héllo\nworld'))
        self.assertEqual((await attachments.extract('../../etc/notes.txt', b'hi'))['name'], 'notes.txt')

    async def test_invalid_utf8_falls_back_to_replacement_characters(self):
        self.assertEqual((await attachments.extract('a.txt', b'ok \xff\xfe end'))['text'], 'ok �� end')

    async def test_text_pdf(self):
        result = await attachments.extract('paper.pdf', pdf_with_text('Synthetic first page.', 'Synthetic second page.'))
        self.assertEqual(result['kind'], 'pdf')
        self.assertIn('Synthetic first page.', result['text'])
        self.assertIn('Synthetic second page.', result['text'])
        self.assertEqual(result['tokens'], -(-result['chars'] // 4))

    async def test_rejections_use_short_friendly_messages(self):
        cases = [
            ('scan.pdf', blank_pdf(), 'no text layer'),
            ('locked.pdf', blank_pdf(password='synthetic'), 'encrypted'),
            ('broken.pdf', b'%PDF-1.4 not really', 'could not read'),
            ('fake.pdf', b'plain text', 'could not read'),
            ('photo.png', PNG, 'images'),
            ('photo.txt', PNG, 'images'),
            ('photo.jpg', b'\xff\xd8\xff\xe0', 'images'),
            ('blob.txt', b'abc\0def', 'binary'),
            ('program.exe', b'MZ', 'only read text'),
            ('empty.txt', b'  \n', 'empty'),
        ]
        for name, data, message in cases:
            with self.assertRaises(AttachmentError, msg=name) as caught:
                await attachments.extract(name, data)
            self.assertIn(message, str(caught.exception), name)

    async def test_size_and_page_caps(self):
        with patch.object(attachments, 'MAX_UPLOAD', 10):
            with self.assertRaises(AttachmentError) as caught:
                await attachments.extract('a.txt', b'x' * 11)
            self.assertEqual(caught.exception.status, 413)
        with patch.object(attachments, 'MAX_PAGES', 2):
            with self.assertRaises(AttachmentError) as caught:
                await attachments.extract('a.pdf', pdf_with_text('one', 'two', 'three'))
            self.assertIn('3 pages', str(caught.exception))
            self.assertTrue((await attachments.extract('a.pdf', pdf_with_text('Synthetic page one has text.', 'Synthetic page two has text.')))['text'])
        with patch.object(attachments, 'MAX_EXTRACTED_CHARS', 5):
            with self.assertRaises(AttachmentError):
                await attachments.extract('a.txt', b'123456')

    async def test_pdf_work_is_bounded_in_time(self):
        workers = []
        spawn = asyncio.create_subprocess_exec

        async def slow_worker(*args, **kwargs):
            worker = await spawn(sys.executable, '-I', '-c', 'import time; time.sleep(60)', **kwargs)
            workers.append(worker)
            return worker

        with patch.object(attachments, 'EXTRACT_SECONDS', .1), \
                patch.object(attachments.asyncio, 'create_subprocess_exec', slow_worker):
            with self.assertRaises(AttachmentError) as caught:
                await attachments.extract('a.pdf', pdf_with_text('Synthetic page with enough text.'))
        self.assertEqual(caught.exception.status, 408)
        self.assertEqual(len(workers), 1)
        self.assertIsNotNone(workers[0].returncode)
        with self.assertRaises(ProcessLookupError):
            os.kill(workers[0].pid, 0)

    async def test_extraction_runs_in_a_reaped_child(self):
        workers = []
        spawn = asyncio.create_subprocess_exec

        async def capture(*args, **kwargs):
            worker = await spawn(*args, **kwargs)
            workers.append(worker)
            return worker

        with patch.object(attachments.asyncio, 'create_subprocess_exec', capture):
            await attachments.extract('a.pdf', pdf_with_text('Synthetic page text for the process check.'))
        self.assertEqual(len(workers), 1)
        self.assertNotEqual(workers[0].pid, os.getpid())
        self.assertEqual(workers[0].returncode, 0)

    async def test_pdf_workers_are_limited_and_cancelled_workers_reaped(self):
        workers = []
        spawn = asyncio.create_subprocess_exec
        started = asyncio.Event()

        async def slow_worker(*args, **kwargs):
            worker = await spawn(sys.executable, '-I', '-c', 'import time; time.sleep(60)', **kwargs)
            workers.append(worker)
            if len(workers) == 2:
                started.set()
            return worker

        with patch.object(attachments.asyncio, 'create_subprocess_exec', slow_worker):
            tasks = [asyncio.create_task(attachments.extract('a.pdf', b'%PDF-1.4')) for _ in range(2)]
            try:
                await asyncio.wait_for(started.wait(), 2)
                with self.assertRaises(AttachmentError) as caught:
                    await attachments.extract('a.pdf', b'%PDF-1.4')
                self.assertEqual(caught.exception.status, 503)
                self.assertIn('busy', str(caught.exception))
                self.assertEqual(len(workers), 2)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        self.assertTrue(all(worker.returncode is not None for worker in workers))


class AttachmentApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await test_chat.ChatTests.asyncSetUp(self)
        self.fake.delay = 0

    asyncTearDown = test_chat.ChatTests.asyncTearDown
    request, create, send, settled, generations, control = (test_chat.ChatTests.request, test_chat.ChatTests.create, test_chat.ChatTests.send,
                                                             test_chat.ChatTests.settled, test_chat.ChatTests.generations, test_chat.ChatTests.control)

    async def upload(self, name, data, *, headers=None, status=200, client=None):
        form = FormData()
        form.add_field('file', data, filename=name, content_type='application/octet-stream')
        url = '/api/attachments/extract' if client is None else self.client.make_url('/api/attachments/extract')
        async with (client or self.client).post(url, data=form, headers=self.headers if headers is None else headers) as response:
            result = await response.json()
            self.assertEqual(response.status, status, result)
            return result

    async def post(self, path, **body):
        return await self.request('POST', path, {'request_id': str(uuid4()), **body}, 200)

    # Extract route

    async def test_extract_returns_text_and_stores_nothing(self):
        result = await self.upload('notes.md', b'# Synthetic\nSome notes.')
        self.assertEqual(result, {'name': 'notes.md', 'kind': 'text', 'chars': 23, 'tokens': 6, 'text': '# Synthetic\nSome notes.'})
        pdf = await self.upload('paper.pdf', pdf_with_text('Synthetic paper with some text.'))
        self.assertEqual((pdf['kind'], pdf['text']), ('pdf', 'Synthetic paper with some text.'))
        self.assertEqual(self.store.count_conversations(), 0)
        self.assertFalse([p for p in self.state.rglob('*') if p.suffix in ('.md', '.pdf')])

    async def test_extract_rejections(self):
        self.assertIn('no text layer', (await self.upload('scan.pdf', blank_pdf(), status=400))['error'])
        self.assertIn('encrypted', (await self.upload('lock.pdf', blank_pdf(password='synthetic'), status=400))['error'])
        self.assertIn('images', (await self.upload('p.png', PNG, status=400))['error'])
        self.assertIn('binary', (await self.upload('b.txt', b'a\0b', status=400))['error'])
        async with self.client.post('/api/attachments/extract', json={}, headers=self.headers) as response:
            self.assertEqual(response.status, 400)
        with patch.object(attachments, 'MAX_UPLOAD', 100):
            self.assertIn('larger', (await self.upload('big.txt', b'x' * 200, status=413))['error'])
            self.assertIn('larger', (await self.upload('big.txt', b'x' * 70000, status=413))['error'])

    async def raw_upload(self, body, content_type, status):
        async with self.client.post('/api/attachments/extract', data=body,
                                    headers={**self.headers, 'Content-Type': content_type}) as response:
            result = await response.json()
            self.assertEqual(response.status, status, result)
            self.assertTrue(result['error'])

    async def test_malformed_multipart_is_a_friendly_400(self):
        cases = [
            (b'body', 'multipart/form-data'),
            (b'body', 'text/plain'),
            (b'body', 'multipart/form-data; boundary=sample'),
            (b'--sample--\r\n', 'multipart/form-data; boundary=sample'),
            (b'--sample\r\nContent-Disposition: form-data; name="other"\r\n\r\nvalue\r\n--sample--\r\n',
             'multipart/form-data; boundary=sample'),
            (b'--sample\r\nContent-Disposition: form-data; name="file"; filename="a.txt"\r\n\r\nunterminated',
             'multipart/form-data; boundary=sample'),
        ]
        for body, content_type in cases:
            await self.raw_upload(body, content_type, 400)

    async def test_extract_accepts_exactly_one_file_part(self):
        form = FormData()
        for name in ('a.txt', 'b.txt'):
            form.add_field('file', b'notes', filename=name)
        async with self.client.post('/api/attachments/extract', data=form, headers=self.headers) as response:
            self.assertEqual(response.status, 400)
            self.assertTrue((await response.json())['error'])

    async def test_chunked_preamble_and_epilogue_count_toward_upload_budget(self):
        file = (b'--sample\r\nContent-Disposition: form-data; name="file"; filename="a.txt"'
                b'\r\n\r\nnotes\r\n--sample--\r\n')
        for preamble in (True, False):
            async def chunks():
                if not preamble:
                    yield file
                for _ in range(80):
                    yield b'x' * 1022 + b'\r\n'
                if preamble:
                    yield file
            with patch.object(attachments, 'MAX_UPLOAD', 1024):
                await self.raw_upload(chunks(), 'multipart/form-data; boundary=sample', 413)

    async def test_upload_read_has_an_overall_deadline(self):
        async def stalled():
            yield b'--sample\r\n'
            await asyncio.sleep(1)
        with patch('roost.handlers.UPLOAD_SECONDS', .05):
            await self.raw_upload(stalled(), 'multipart/form-data; boundary=sample', 408)

    async def test_extract_requires_session_and_origin(self):
        async with ClientSession() as anonymous:
            self.assertEqual((await self.upload('a.txt', b'hi', client=anonymous, status=401))['error'], 'Sign in to Roost.')
        await self.upload('a.txt', b'hi', headers={'X-Roost-Request': '1'}, status=403)
        await self.upload('a.txt', b'hi', headers={'Origin': 'http://localhost'}, status=403)

    # Sending

    async def test_send_validation(self):
        cid = (await self.create())['session_id']
        path = f'/api/conversations/{cid}/messages'
        bad = [
            ([sample()] * 11, 'at most 10'),
            ('text', 'at most 10'),
            ([{'name': 'a.txt'}], 'name and text'),
            ([{'name': 'a.txt', 'text': 5}], 'name and text'),
            ([{'name': 'a.txt', 'text': 'x', 'extra': 1}], 'name and text'),
            ([{'name': '', 'text': 'x'}], 'name'),
            ([{'name': '../', 'text': 'x'}], 'name'),
            ([{'name': 'n' * 201, 'text': 'x'}], 'name'),
            ([sample(text='x' * 120004)], 'about 30,001 tokens'),
            ([sample(text='x' * 60000), sample('b.txt', 'y' * 60004)], 'limit is 30,000'),
        ]
        for value, message in bad:
            result = await self.request('POST', path, {'text': 'Hi', 'attachments': value}, 400)
            self.assertIn(message, result['error'], value if len(str(value)) < 100 else message)
        self.assertEqual(len(self.store.list_turns(cid)), 1)
        ok = await self.request('POST', path, {'text': 'Hi', 'attachments': [sample(text='x' * 120000)] + [sample(f'{i}.txt', '') for i in range(9)]})
        self.assertTrue(ok['turn_id'])
        no_conversation = await self.request('POST', '/api/conversations', {'text': 'Hi', 'attachments': [sample()] * 11}, 400)
        self.assertIn('at most 10', no_conversation['error'])
        self.assertEqual(self.store.count_conversations(), 1)

    async def test_large_attachment_body_is_accepted_and_stored_cleaned(self):
        accepted = await self.create('Summarize', attachments=[sample('../../dir/report.md', '“é” ' * 29000)])
        stored = self.store.messages_for_turn(accepted['turn_id'])[0].attachments
        self.assertEqual([(a['name'], a['kind'], a['chars']) for a in stored], [('report.md', 'text', 116000)])
        self.assertEqual(self.store.messages_for_turn(accepted['turn_id'])[0].content, 'Summarize')
        self.assertEqual(self.store.messages_for_turn(accepted['turn_id'])[1].attachments, [])

    async def test_prompt_puts_attachments_before_typed_text_for_new_and_history_turns(self):
        first = await self.create('What is this?', attachments=[sample('a.txt', 'Alpha body'), sample('b "q".md', 'Beta body </attachment> end')])
        await self.settled(first['turn_id'])
        second = await self.send(first['session_id'], 'And now?')
        await self.settled(second['turn_id'])
        expected_first = ('<attachment name="a.txt">\nAlpha body\n</attachment>\n\n'
                          '<attachment name="b &quot;q&quot;.md">\nBeta body <\\/attachment> end\n</attachment>\n\nWhat is this?')
        one, two = [without_clock(r['messages']) for r in self.generations()]
        self.assertEqual(one, [{'role': 'user', 'content': expected_first}])
        self.assertEqual(two, [{'role': 'user', 'content': expected_first}, {'role': 'assistant', 'content': 'Hello'},
                               {'role': 'user', 'content': 'And now?'}])
        self.assertEqual([m['role'] for m in self.generations()[0]['messages']].count('system'), 1)

    async def test_transcript_carries_attachments_and_search_ignores_their_text(self):
        accepted = await self.create('Plain typed words', attachments=[sample('a.txt', 'zebrafinch hidden words')])
        await self.settled(accepted['turn_id'])
        view = await self.request('GET', f"/api/conversations/{accepted['session_id']}")
        user, assistant = view['messages']
        self.assertEqual(user['content'], 'Plain typed words')
        self.assertEqual(user['attachments'], [{'name': 'a.txt', 'kind': 'text', 'chars': 23, 'text': 'zebrafinch hidden words'}])
        self.assertNotIn('attachments', assistant)
        self.assertEqual((await self.request('GET', '/api/search?q=zebrafinch'))['sessions'], [])
        self.assertTrue((await self.request('GET', '/api/search?q=typed'))['sessions'])
        exported = await self.request('GET', f"/api/conversations/{accepted['session_id']}/export")
        self.assertEqual(exported['messages'][0]['attachments'][0]['name'], 'a.txt')

    async def test_request_fingerprint_includes_attachments(self):
        key = str(uuid4())
        body = {'text': 'Hi', 'request_id': key, 'attachments': [sample()]}
        first = await self.request('POST', '/api/conversations', body, 201)
        again = await self.request('POST', '/api/conversations', body, 201)
        self.assertEqual(first['turn_id'], again['turn_id'])
        self.assertEqual(self.store.count_conversations(), 1)
        for changed in ({**body, 'attachments': [sample(text='Different')]}, {**body, 'attachments': [sample('other.txt')]},
                        {**body, 'attachments': []}, {k: v for k, v in body.items() if k != 'attachments'}):
            self.assertIn('different payload', (await self.request('POST', '/api/conversations', changed, 409))['error'])
        path = f"/api/conversations/{first['session_id']}/messages"
        second = {'text': 'More', 'request_id': str(uuid4()), 'attachments': [sample()]}
        sent = await self.request('POST', path, second)
        self.assertEqual((await self.request('POST', path, second))['turn_id'], sent['turn_id'])
        await self.request('POST', path, {**second, 'attachments': [sample(text='Else')]}, 409)

    # Edit and regenerate

    async def test_edit_keeps_replaces_and_removes_attachments_and_regenerate_reuses_them(self):
        first = await self.create('Original', attachments=[sample('keep.txt', 'Kept body')])
        cid = first['session_id']
        await self.settled(first['turn_id'])
        kept = await self.control(cid, 'edit', first['turn_id'], text='Edited', request_id=str(uuid4()))
        await self.settled(kept['turn_id'])
        self.assertEqual([a['name'] for a in self.store.messages_for_turn(kept['turn_id'])[0].attachments], ['keep.txt'])
        self.assertIn('Kept body', self.generations()[-1]['messages'][-1]['content'])
        replaced = await self.control(cid, 'edit', kept['turn_id'], text='Again', request_id=str(uuid4()),
                                      attachments=[sample('new.md', 'New body')])
        await self.settled(replaced['turn_id'])
        self.assertEqual([a['name'] for a in self.store.messages_for_turn(replaced['turn_id'])[0].attachments], ['new.md'])
        last = self.generations()[-1]['messages'][-1]['content']
        self.assertIn('New body', last)
        self.assertNotIn('Kept body', last)
        await self.control(cid, 'regenerate', replaced['turn_id'])
        await self.settled(replaced['turn_id'])
        self.assertEqual(self.generations()[-1]['messages'][-1]['content'], last)
        removed = await self.control(cid, 'edit', replaced['turn_id'], text='Plain', request_id=str(uuid4()), attachments=[])
        await self.settled(removed['turn_id'])
        self.assertEqual(self.store.messages_for_turn(removed['turn_id'])[0].attachments, [])
        self.assertEqual(without_clock(self.generations()[-1]['messages']), [{'role': 'user', 'content': 'Plain'}])
        await self.request('POST', f'/api/conversations/{cid}/turns/edit',
                           {'turn_id': removed['turn_id'], 'text': 'x', 'request_id': str(uuid4()), 'attachments': [sample()] * 11}, 400)

    async def test_edit_retry_with_different_attachments_conflicts(self):
        first = await self.create('Original', attachments=[sample()])
        await self.settled(first['turn_id'])
        key = str(uuid4())
        body = {'turn_id': first['turn_id'], 'text': 'Edited', 'request_id': key, 'attachments': [sample('b.txt')]}
        path = f"/api/conversations/{first['session_id']}/turns/edit"
        accepted = await self.request('POST', path, body)
        self.assertEqual((await self.request('POST', path, body))['turn_id'], accepted['turn_id'])
        await self.request('POST', path, {**body, 'attachments': []}, 409)


class AttachmentCompactionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await test_chat.CompactionTests.asyncSetUp(self)

    asyncTearDown = test_chat.CompactionTests.asyncTearDown
    web_fixture = test_chat.CompactionTests.web_fixture
    request, create, send, settled, generations, control = (test_chat.CompactionTests.request, test_chat.CompactionTests.create, test_chat.CompactionTests.send,
                                                             test_chat.CompactionTests.settled, test_chat.CompactionTests.generations, test_chat.CompactionTests.control)
    set_context, extend, summary_requests, run_turn = (test_chat.CompactionTests.set_context, test_chat.CompactionTests.extend,
                                                       test_chat.CompactionTests.summary_requests, test_chat.CompactionTests.run_turn)

    async def test_folded_turns_contribute_their_attachment_text(self):
        cid = self.store.create_conversation(model='example-model').id
        accepted = self.store.accept_turn(cid, 'seed-a', 'q1 typed', model='example-model',
                                          attachments=[{'name': 'big.txt', 'kind': 'text', 'chars': 20, 'text': 'MARKER inside a file'}])
        self.store.set_turn_state(accepted.turn_id, 'running')
        self.store.checkpoint_assistant(accepted.turn_id, 'a1 reply')
        self.store.finish(accepted.turn_id, {'usage': {'prompt_tokens': 300}})
        self.extend(cid, 5)
        await self.run_turn(cid)
        folded = self.summary_requests()[0]['messages'][1]['content']
        self.assertIn('<attachment name="big.txt">\nMARKER inside a file\n</attachment>\n\nq1 typed', folded)
        self.assertEqual(self.store.summaries(cid)[0]['through_ordinal'], 4)

    async def test_new_attachment_counts_toward_the_compaction_trigger(self):
        cid = self.store.create_conversation(model='example-model').id
        self.extend(cid, 5, prompt_tokens=100)
        self.assertEqual(self.summary_requests(), [])
        sent = await self.send(cid, 'Short typed text')
        await self.settled(sent['turn_id'])
        self.assertEqual(self.summary_requests(), [])  # 100 + a few tokens is well below 300 of 400
        big = await self.request('POST', f'/api/conversations/{cid}/messages',
                                 {'text': 'Short typed text', 'request_id': str(uuid4()), 'attachments': [sample('long.txt', 'z' * 1400)]})
        await self.settled(big['turn_id'])
        self.assertEqual(len(self.summary_requests()), 1)

    async def test_oversize_attachment_is_cut_for_the_summary_not_dropped(self):
        cid = self.store.create_conversation(model='example-model').id
        accepted = self.store.accept_turn(cid, 'seed-big', 'q1 typed', model='example-model',
                                          attachments=[{'name': 'huge.txt', 'kind': 'text', 'chars': 4000, 'text': 'HEAD' + 'm' * 3992 + 'TAIL'}])
        self.store.set_turn_state(accepted.turn_id, 'running')
        self.store.checkpoint_assistant(accepted.turn_id, 'a1 reply')
        self.store.finish(accepted.turn_id, {'usage': {'prompt_tokens': 300}})
        self.extend(cid, 5)
        await self.run_turn(cid)
        folded = self.summary_requests()[0]['messages'][1]['content']
        self.assertIn('HEAD', folded)
        self.assertIn('TAIL', folded)
        self.assertLess(len(folded), 3000)


class AttachmentStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.directory.name) / 'native.sqlite3')

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def test_migration_six_up_and_down_with_data_surviving(self):
        self.store.migrate(5)
        conversation = self.store.create_conversation()
        old = self.store.accept_turn(conversation.id, 'old', 'Typed before', model='example-model')
        self.assertFalse(self.store.db.execute("SELECT 1 FROM pragma_table_info('messages') WHERE name='attachments'").fetchone())
        self.assertEqual(self.store.get_message(old.message_id).attachments, [])
        self.store.migrate()
        self.assertEqual(self.store.get_message(old.message_id).attachments, [])
        files = [{'name': 'a.txt', 'kind': 'text', 'chars': 5, 'text': 'Hello'}]
        new = self.store.accept_turn(conversation.id, 'new', 'Typed after', model='example-model', attachments=files)
        self.assertEqual(self.store.get_message(new.message_id).attachments, files)
        self.assertEqual(self.store.get_message(new.message_id).content, 'Typed after')
        with self.assertRaises(StoreError):
            self.store.migrate(5)  # a rollback never discards attachments
        self.assertEqual(self.store.get_message(new.message_id).attachments, files)
        self.assertEqual(self.store.db.execute("SELECT version FROM schema_version").fetchone()[0], 7)  # the refused rollback left every migration in place
        self.store.cancel_queued(new.turn_id)
        self.store.migrate(5)
        self.assertEqual(self.store.get_message(old.message_id).content, 'Typed before')
        self.store.migrate()
        self.assertEqual(self.store.get_message(old.message_id).attachments, [])

    def test_content_search_index_excludes_attachment_text(self):
        conversation = self.store.create_conversation()
        self.store.accept_turn(conversation.id, 'r', 'typed words', model='m',
                               attachments=[{'name': 'a.txt', 'kind': 'text', 'chars': 6, 'text': 'quokka'}])
        self.assertTrue(self.store.search('typed'))
        self.assertEqual(self.store.search('quokka'), [])


if __name__ == '__main__':
    unittest.main()
