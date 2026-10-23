FROM python:3.12-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 ROOST_STATE_DIR=/var/lib/roost
WORKDIR /app
COPY pyproject.toml LICENSE ./
COPY LICENSES ./LICENSES
COPY roost ./roost
RUN python -m pip install --no-cache-dir .

USER 10001:10001
EXPOSE 8787
ENTRYPOINT ["python", "-m", "roost"]
CMD ["serve", "--host", "0.0.0.0"]
