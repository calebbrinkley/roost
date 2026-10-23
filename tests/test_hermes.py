"""The Hermes module at its interface: concepts in, normalised records out.

Most tests run against the demo's Hermes stand-in over HTTP; the response variants real Hermes
versions return run against a small upstream that answers exactly what each test needs.
"""

import base64
import tempfile
import unittest
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestServer

from dev import demo
from roost.hermes import Hermes, HermesError


class Upstream(unittest.IsolatedAsyncioTestCase):
    async def connect(self, app: web.Application) -> Hermes:
        self.directory = tempfile.TemporaryDirectory()
        password = Path(self.directory.name) / "password"
        password.write_text(demo.HERMES_PASSWORD + "\n")
        self.upstream = TestServer(app)
        await self.upstream.start_server()
        hermes = Hermes(str(self.upstream.make_url("")), "demo", password)
        await hermes.start()
        self.addAsyncCleanup(self.cleanup, hermes)
        return hermes

    async def cleanup(self, hermes):
        await hermes.close()
        await self.upstream.close()
        self.directory.cleanup()


class DemoHermesTests(Upstream):
    async def asyncSetUp(self):
        self.hermes = await self.connect(demo.mock_hermes())

    async def test_conversations_list_read_rename_and_delete(self):
        page = await self.hermes.sessions()
        ids = [item["id"] for item in page["sessions"]]
        self.assertIn("roost_benchmark", ids)
        self.assertFalse(any(sid.startswith("cron_") for sid in ids), "job runs stay out of Messages")
        self.assertEqual((await self.hermes.sessions(offset=len(ids)))["sessions"], [])
        transcript = await self.hermes.messages("roost_benchmark")
        self.assertEqual(transcript["messages"][0]["role"], "user")
        self.assertEqual((await self.hermes.history("roost_benchmark", offset=0, limit=500))[0]["id"], 1)
        await self.hermes.rename_session("roost_benchmark", "Benchmark")
        self.assertEqual(next(item for item in (await self.hermes.sessions())["sessions"]
                              if item["id"] == "roost_benchmark")["title"], "Benchmark")
        await self.hermes.delete_session("roost_benchmark")
        with self.assertRaises(HermesError) as caught:
            await self.hermes.messages("roost_benchmark")
        self.assertEqual(caught.exception.status, 404)

    async def test_jobs_are_wrapped_and_keep_their_ids_through_every_change(self):
        jobs = await self.hermes.jobs()
        self.assertIn("job_morning", [job["id"] for job in jobs["jobs"]])
        self.assertEqual((await self.hermes.job("job_morning"))["id"], "job_morning")
        runs = await self.hermes.runs("job_morning")
        self.assertEqual(runs["runs"][0]["id"], "cron_job_morning_sample")
        created = await self.hermes.create_job({"name": "Tea", "prompt": "Remind me.", "schedule": "0 16 * * *", "deliver": "local"})
        self.assertTrue(created["id"].startswith("job_"))
        self.assertEqual((await self.hermes.update_job(created["id"], {"name": "Tea time"}))["name"], "Tea time")
        self.assertEqual((await self.hermes.job_action(created["id"], "pause"))["state"], "paused")
        await self.hermes.delete_job(created["id"])
        self.assertNotIn(created["id"], [job["id"] for job in (await self.hermes.jobs())["jobs"]])

    async def test_profile_soul_and_the_rest_of_the_dashboard(self):
        self.assertEqual((await self.hermes.profile())["display_name"], "Kestrel")
        await self.hermes.save_soul("Be brief.")
        self.assertEqual(await self.hermes.soul(), "Be brief.")
        self.assertEqual((await self.hermes.status())["version"], "demo")
        await self.hermes.restart()
        self.assertEqual(await self.hermes.transcribe("data:audio/webm;base64,AAAA", "audio/webm"),
                         "This is the demo transcript of a voice message.")
        spoken = await self.hermes.speak("Hello.")
        self.assertEqual(base64.b64decode(spoken.partition(",")[2])[:4], b"RIFF")

    async def test_unsafe_ids_and_actions_never_reach_hermes(self):
        for call in (lambda: self.hermes.messages("../config"), lambda: self.hermes.delete_session(".."),
                     lambda: self.hermes.job("a/b"), lambda: self.hermes.job_action("job_morning", "delete")):
            with self.assertRaises(HermesError) as caught:
                await call()
            self.assertIn(caught.exception.status, {400, 404})
        self.assertEqual((await self.hermes.job("job_morning"))["state"], "scheduled")


class ResponseVariantTests(Upstream):
    """What different Hermes versions answer, and the records Roost makes of it."""

    async def serve(self, routes: dict) -> Hermes:
        self.paths = []
        app = web.Application()

        async def login(request):
            response = web.json_response({"ok": True})
            response.set_cookie("demo_hermes", "signed-in")
            return response

        async def answer(request):
            self.paths.append(request.raw_path)
            return web.json_response(routes[request.path])
        app.router.add_post("/auth/password-login", login)
        for path in routes:
            app.router.add_route("*", path, answer)
        return await self.connect(app)

    async def test_jobs_keyed_by_job_id_or_wrapped_get_an_id(self):
        hermes = await self.serve({"/api/cron/jobs": {"jobs": [{"job_id": "one"}, {"id": "two"}, {"name": "no id"}, "junk"]}})
        self.assertEqual([job["id"] for job in (await hermes.jobs())["jobs"]], ["one", "two"])

    async def test_conversations_prefer_session_id_and_drop_unusable_ids(self):
        hermes = await self.serve({"/api/sessions": {"sessions": [{"session_id": "chat:one", "id": 7}, {"id": ".."}], "total": 2}})
        page = await hermes.sessions()
        self.assertEqual(([item["id"] for item in page["sessions"]], page["total"]), (["chat:one"], 2))

    async def test_runs_without_a_conversation_are_kept_without_an_id(self):
        hermes = await self.serve({"/api/cron/jobs/one/runs": [{"session_id": "run:a"}, {"title": "no id"}, {"id": "../x", "title": "bad"}]})
        runs = (await hermes.runs("one"))["runs"]
        self.assertEqual(runs, [{"session_id": "run:a", "id": "run:a"}, {"title": "no id"}, {"title": "bad"}])
        self.assertEqual(self.paths, ["/api/cron/jobs/one/runs?limit=20"])

    async def test_an_id_reaches_hermes_as_one_path_segment(self):
        hermes = await self.serve({"/api/sessions/chat:one/messages": {"messages": []}})
        await hermes.messages("chat:one")
        self.assertEqual(self.paths, ["/api/sessions/chat:one/messages?inline_images=false"])

    async def test_an_unusual_profile_name_reads_as_default(self):
        hermes = await self.serve({"/api/profiles/active": {"current": "../config"},
                                   "/api/profiles/default/soul": {"content": "soul"}})
        self.assertEqual(await hermes.soul(), "soul")
        self.assertEqual(self.paths[-1], "/api/profiles/default/soul")

    async def test_a_transcript_without_messages_is_invalid(self):
        hermes = await self.serve({"/api/sessions/one/messages": {"error": "gone"}})
        with self.assertRaises(HermesError) as caught:
            await hermes.messages("one")
        self.assertEqual(caught.exception.status, 502)


if __name__ == "__main__":
    unittest.main()
