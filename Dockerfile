FROM python:3.14-slim

ARG UID=1000
ARG GID=1000

RUN set -e; \
    if [ "${GID}" -ne 0 ] && ! getent group "${GID}" >/dev/null 2>&1; then \
        groupadd -g ${GID} appuser; \
    fi; \
    if [ "${UID}" -ne 0 ] && ! id -u "${UID}" >/dev/null 2>&1; then \
        useradd -m -u ${UID} -g ${GID} -d /home/appuser appuser; \
    fi

WORKDIR /app

RUN rm -rf /var/lib/apt/lists/* \
    && apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies first (pyproject.toml + the claude_chatter package) so
# the pip layer is cached independently of test_suite.py edits.
COPY pyproject.toml bridge_mcp.py ./
COPY claude_chatter/ ./claude_chatter/
RUN pip install --no-cache-dir "git+https://github.com/non7top/claude-chatter.git@v0.2.0" \
    && pip install --no-cache-dir . pytest

COPY test_suite.py ./

# Give this container its OWN $HOME (with its own ~/.claude/sessions), entirely
# separate from the host's. claude_chatter reads/writes ~/.claude/sessions for
# real session discovery - tests must never be able to see or mutate a
# developer's actual live sessions.
RUN mkdir -p /home/appuser/.claude/sessions /home/appuser/.claude/projects \
    && chown -R ${UID}:${GID} /home/appuser /app

USER appuser
ENV HOME=/home/appuser

CMD ["python", "-m", "pytest", "test_suite.py", "-v"]
