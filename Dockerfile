# Deterministic build.
#
# The previous deploy crash-looped on `ModuleNotFoundError: No module named
# 'uvicorn'` because the Nixpacks build never ran pip install - it copied the
# source to /app and started it against a bare interpreter. A Dockerfile
# removes the guesswork: if the install fails, the *build* fails, loudly,
# instead of producing an image that dies on every boot.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first, so source edits don't invalidate the install layer.
COPY requirements.txt .

# setuptools is pinned explicitly so any sdist dependency can build; nothing
# here needs a C compiler.
RUN pip install --upgrade pip setuptools wheel \
 && pip install -r requirements.txt

# Fail the BUILD if anything is missing, rather than discovering it at runtime
# in a restart loop. This is the check whose absence caused the last outage.
RUN python -c "import uvicorn, fastapi, jinja2, pandas, numpy, apscheduler, \
sqlalchemy, psycopg2, pytz, httpx, dotenv, kiteconnect; \
print('all runtime dependencies present')"

COPY . .

# Byte-compile everything so import errors surface at build time too.
RUN python -m compileall -q . && echo "sources compile"

EXPOSE 8000

# Web by default. The worker service runs the same image with RUN_MODE=worker.
CMD ["python", "main.py"]
