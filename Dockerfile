FROM python:3.14-slim

ARG UID=1000
ARG GID=1000

RUN set -e; \
    if ! getent group "${GID}" >/dev/null; then groupadd -g "${GID}" appuser; fi; \
    if ! getent passwd "${UID}" >/dev/null; then useradd -m -u "${UID}" -g "${GID}" -d /home/appuser appuser; fi

WORKDIR /app

# Install dependencies first (pyproject.toml + the claude_chatter_mcp package) so
# the pip layer is cached independently of test_suite.py edits.
COPY pyproject.toml README.md LICENSE bridge_mcp.py ./
COPY claude_chatter_mcp/ ./claude_chatter_mcp/
RUN pip install --no-cache-dir build pytest \
    && pip install --no-cache-dir .

COPY test_suite.py ./

# Give this container its OWN $HOME (with its own ~/.claude/sessions), entirely
# separate from the host's. claude_chatter (the library) reads/writes ~/.claude/sessions for
# real session discovery - tests must never be able to see or mutate a
# developer's actual live sessions.
RUN mkdir -p /home/appuser/.claude/sessions /home/appuser/.claude/projects \
    && chown -R "${UID}:${GID}" /home/appuser /app

USER ${UID}:${GID}
ENV HOME=/home/appuser

CMD ["python", "-m", "pytest", "test_suite.py", "-v"]
