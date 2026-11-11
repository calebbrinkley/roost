FROM python:3.15-slim-bookworm@sha256:575f8bc12ec26542e0678bc6b60e385218248019d433157ec327264a78c3a853

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
