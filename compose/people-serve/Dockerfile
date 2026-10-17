# people serve - the people tool's operations as a JSON API for the dashboard's
# People page (scripts/people/serve.py, docs/runbook.md "People serve").
#
# stdlib only: no pip. The build context is scripts/ (compose/docker-compose.yml),
# and the layout is kept (people/ beside identity/) because people.py loads
# ../identity/logto-admin.py. State (the Logto admin ledger) is the /var/lib/people
# volume; the directory is created owned by nobody so a fresh named volume is
# writable by the unprivileged user.

FROM python:3.12-alpine

WORKDIR /app/people
COPY people/*.py people/rights.json people/invite-email.txt /app/people/
COPY identity/logto-admin.py /app/identity/logto-admin.py
RUN mkdir -p /var/lib/people && chown nobody:nobody /var/lib/people

ENV PYTHONUNBUFFERED=1 \
    LOGTO_ADMIN_LEDGER=/var/lib/people/logto-admin.ledger.jsonl
EXPOSE 8095

USER nobody
CMD ["python", "serve.py", "--port", "8095"]
