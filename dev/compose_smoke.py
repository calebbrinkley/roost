"""Exercise the shipped Compose install with disposable state and a containerized OpenAI-compatible provider."""

import http.cookies
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "https://roost.example.invalid"
PASSWORD = "example-compose-password"


def main():
    if os.getuid() == 0:
        raise SystemExit("Run the Compose smoke check from an unprivileged Docker-enabled account.")
    project = "roost-smoke-" + uuid.uuid4().hex[:12]
    with tempfile.TemporaryDirectory(prefix="roost-compose-") as directory:
        private = Path(directory)
        state, secret = private / "state", private / "model-key"
        state.mkdir(mode=0o700)
        secret.write_text("example-model-key\n")
        secret.chmod(0o600)
        env_file = private / "compose.env"
        values = {"ROOST_PUBLIC_ORIGIN": ORIGIN, "ROOST_MODEL_URL": "http://provider:18719/v1",
                  "ROOST_MODEL_KEY_SOURCE": str(secret),
                  "ROOST_STATE_PATH": str(state), "ROOST_UID": str(os.getuid()),
                  "ROOST_GID": str(os.getgid()), "ROOST_PORT": "0"}
        env_file.write_text("".join(f"{key}={value}\n" for key, value in values.items()))
        env_file.chmod(0o600)
        override = private / "compose-test.json"
        image = project + ":test"
        program = """
import sys
sys.path.insert(0, '/fixtures')
from aiohttp import web
from openai_fake import OpenAIFake
from speech_fake import SpeechFake
from web_fake import WebFake
fake, speech, search = OpenAIFake(), SpeechFake(), WebFake()
app = fake.server.app
app.router.add_post('/v1/audio/transcriptions', speech.transcribe)
app.router.add_post('/v1/audio/speech', speech.speak)
app.router.add_get('/search', search.search)
web.run_app(app, host='0.0.0.0', port=18719, access_log=None)
"""
        override.write_text(json.dumps({"services": {
            "roost": {"image": image, "depends_on": ["provider"]},
            "provider": {"image": image, "entrypoint": ["python", "-c", program], "command": [],
                         "volumes": [str(ROOT / "tests") + ":/fixtures:ro"]}}}))
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith(("ROOST_", "COMPOSE_"))}
        compose = ["docker", "compose", "--project-name", project, "--env-file", str(env_file),
                   "-f", str(ROOT / "compose.yaml"), "-f", str(override)]

        def run(*args, input=None):
            return subprocess.run([*compose, *args], cwd=ROOT, env=environment, input=input, text=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=True).stdout

        def address():
            published = run("port", "roost", "8787").strip()
            assert published.startswith("127.0.0.1:"), "Roost must publish only on loopback."
            return "http://" + published

        def request(base, path, *, method="GET", body=None, cookie=None):
            headers = {"Origin": ORIGIN, "X-Roost-Request": "1"}
            if body is not None:
                headers["Content-Type"] = "application/json"
            if cookie:
                headers["Cookie"] = cookie
            req = urllib.request.Request(base + path, method=method, headers=headers,
                                         data=json.dumps(body).encode() if body is not None else None)
            try:
                response = urllib.request.urlopen(req, timeout=10)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                return response.status, json.load(response), response.headers

        try:
            run("config", "--quiet")
            run("build", "roost")
            run("run", "--rm", "--no-deps", "-T", "roost", "set-password", input=PASSWORD + "\n" + PASSWORD + "\n")
            saved_hash = (state / "password.hash").read_bytes()
            assert (state / "password.hash").stat().st_mode & 0o777 == 0o600
            run("up", "-d", "--wait", "--wait-timeout", "60")
            base = address()
            assert request(base, "/api/health")[:2] == (200, {"status": "ready"})
            assert request(base, "/api/conversations")[0] == 401
            status, _, headers = request(base, "/api/login", method="POST", body={"password": PASSWORD})
            assert status == 200
            cookies = http.cookies.SimpleCookie(headers["Set-Cookie"])
            session = cookies["roost_session"]
            assert session["secure"] and session["httponly"] and session["samesite"] == "Strict"
            cookie = "roost_session=" + session.value
            status, data, _ = request(base, "/api/conversations", cookie=cookie)
            assert status == 200 and data["sessions"] == []
            status, discovered, _ = request(base, "/api/settings/servers/primary/discover", method="POST", cookie=cookie)
            assert status == 200 and discovered["models"][0]["id"] == "example-model"
            assert request(base, "/api/settings/servers/primary/models", method="PUT", body={
                "models": [{"id": "example-model", "display_name": "Example", "enabled": True,
                            "thinking_mode": "unsupported"}], "default_model": "example-model"}, cookie=cookie)[0] == 200
            status, data, _ = request(base, "/api/conversations", method="POST", body={"text": "Compose smoke check"}, cookie=cookie)
            assert status == 201
            conversation = data["session_id"]
            for _ in range(50):
                status, data, _ = request(base, "/api/conversations/" + conversation, cookie=cookie)
                if status == 200 and data["messages"][-1]["content"] == "Hello" and not data["working"]:
                    break
                time.sleep(0.2)
            else:
                raise AssertionError("The container did not receive the provider's completed reply.")
            assert request(base, "/api/settings", method="PATCH", body={"accent": "rose", "search_url": "http://provider:18719"}, cookie=cookie)[0] == 200
            assert request(base, "/api/settings/web-search/test", method="POST", body={"query": "example"}, cookie=cookie)[1]["results"]
            assert request(base, "/api/audio/transcribe", method="POST", body={"data_url": "data:audio/mp4;base64,Y2xpcA==",
                "mime_type": "audio/mp4", "duration_seconds": 1}, cookie=cookie)[1] == {"transcript": "Hello"}
            assert request(base, "/api/memory", method="POST", body={"text": "A synthetic compose preference."}, cookie=cookie)[0] == 201
            status, job, _ = request(base, "/api/jobs", method="POST", body={"name": "Test report", "prompt": "Say hello",
                "schedule": {"kind": "every", "value": 1, "unit": "hours"}}, cookie=cookie)
            assert status == 201
            assert request(base, "/api/jobs/" + job["id"] + "/trigger", method="POST", cookie=cookie)[0] == 200
            for _ in range(50):
                runs = request(base, "/api/jobs/" + job["id"] + "/runs", cookie=cookie)[1]["runs"]
                if runs and runs[0]["state"] == "completed":
                    break
                time.sleep(.2)
            else:
                raise AssertionError("The container did not finish its scheduled job.")
            assert run("exec", "-T", "roost", "python", "-c", "import os; print(os.getuid())").strip() == str(os.getuid())
            container_id = run("ps", "-q", "roost").strip()
            details = json.loads(subprocess.check_output(["docker", "inspect", container_id]))[0]
            assert details["HostConfig"]["ReadonlyRootfs"] is True
            mounts = [mount for mount in details["Mounts"] if mount["Destination"] == "/run/secrets/model_key"]
            assert len(mounts) == 1 and not mounts[0]["RW"]
            run("restart", "roost")
            run("up", "-d", "--wait", "--wait-timeout", "60")
            assert request(address(), "/api/settings", cookie=cookie)[1]["accent"] == "rose"
            run("up", "-d", "--force-recreate", "--wait", "--wait-timeout", "60", "roost")
            assert run("ps", "-q", "roost").strip() != container_id
            assert request(address(), "/api/settings", cookie=cookie)[1]["accent"] == "rose"
            assert (state / "password.hash").read_bytes() == saved_hash
            print("PASS: Compose build, password setup, health, owner authentication, model reply, search, speech, memory, job run, "
                  "non-root/read-only execution, secret mount, restart and recreation persistence.")
        except subprocess.CalledProcessError as error:
            subprocess.run([*compose, "logs", "--no-color", "--tail", "50"], cwd=ROOT, env=environment, check=False)
            raise RuntimeError(f"Compose command failed (exit {error.returncode}):\n{error.stdout}") from None
        finally:
            subprocess.run([*compose, "down", "--remove-orphans"], cwd=ROOT, env=environment, check=False)
            subprocess.run(["docker", "image", "rm", image], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


if __name__ == "__main__":
    main()
