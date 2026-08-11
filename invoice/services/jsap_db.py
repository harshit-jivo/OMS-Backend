"""Read-only access to the JSAP application's SQL Server database.

The DSR/JSAP credit-limit service exposes an approval flow at
``/api/CreditLimit/GetApprovalFlow/?flowId=...`` but offers no endpoint that maps
a credit-limit document to its flow id. The only way to get one over HTTP is to
pull a month's worth of documents from ``GetAllDocuments`` and scan for the id,
which fails as soon as the request is older than the month being queried.

The flow id is a plain column in the JSAP database, so resolve it there instead:

    cl.jsCreditDocument.id   -- the "creditDocumentId" returned when a request is
                                created; stored on CreditLimitLogs.jsap_doc_id
    cl.jsFlow.userDocumentId -- points back at that document
    cl.jsFlow.id             -- IS the flowId (matches cl.jsFlowStatus.flowId)

Connection handling mirrors sap_sync.services.connection.SAPConnection, including
the several server/port spellings FreeTDS accepts — only some of them work
against this host.
"""

import logging

import pymssql
from django.conf import settings

logger = logging.getLogger(__name__)


class JSAPConnection:
    """Minimal pymssql wrapper for the JSAP database.

    Use as a context manager so the connection is always closed:

        with JSAPConnection() as jsap:
            flow_id = jsap.get_credit_flow_id(doc_id)
    """

    @staticmethod
    def _clean(value):
        if value is None:
            return value
        return str(value).strip().strip("'").strip('"')

    def __init__(self):
        self.host = self._clean(getattr(settings, 'JSAP_DB_HOST', '103.89.45.75'))
        self.port = int(getattr(settings, 'JSAP_DB_PORT', 1433))
        self.database = self._clean(getattr(settings, 'JSAP_DB_NAME', 'jsaplive3'))
        self.username = self._clean(getattr(settings, 'JSAP_DB_USER', 'ab'))
        self.password = self._clean(getattr(settings, 'JSAP_DB_PASSWORD', ''))
        self.connection = None
        self.cursor = None

    def connect(self):
        # Same fallback ladder as SAPConnection: this host only answers to the
        # separate host/port form, but the others are kept so a move to a named
        # instance or a different FreeTDS build keeps working.
        attempts = [
            {"server": self.host, "port": self.port},
            {"server": f"{self.host},{self.port}"},
            {"server": f"{self.host}:{self.port}"},
        ]
        last_error = None

        for params in attempts:
            try:
                self.connection = pymssql.connect(
                    user=self.username,
                    password=self.password,
                    database=self.database,
                    timeout=30,
                    login_timeout=15,
                    **params,
                )
                self.cursor = self.connection.cursor(as_dict=True)
                return True
            except Exception as exc:
                last_error = exc
                logger.warning("JSAP DB connect attempt failed (%s): %s", params, exc)

        raise ConnectionError(
            f"JSAP connection failed ({self.host}:{self.port}/{self.database}): {last_error}"
        )

    def disconnect(self):
        if self.cursor:
            self.cursor.close()
            self.cursor = None
        if self.connection:
            self.connection.close()
            self.connection = None

    def execute_query(self, query, params=None):
        if not self.connection:
            self.connect()
        self.cursor.execute(query, params)
        return self.cursor.fetchall()

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()

    def get_credit_flow_id(self, credit_document_id):
        """Approval flow id for a credit-limit document, or None if it has none.

        A flow row is created when the request enters the approval workflow, so a
        just-submitted document can legitimately have no flow yet. Newest flow
        wins on the off chance a document was resubmitted into a second one.
        """
        try:
            document_id = int(credit_document_id)
        except (TypeError, ValueError):
            return None

        rows = self.execute_query(
            """
            SELECT TOP 1 f.id AS flowId
            FROM cl.jsFlow f
            WHERE f.userDocumentId = %s
            ORDER BY f.id DESC
            """,
            (document_id,),
        )
        return rows[0]['flowId'] if rows else None


def get_credit_flow_id(credit_document_id):
    """Convenience wrapper — opens and closes a connection for a single lookup."""
    with JSAPConnection() as jsap:
        return jsap.get_credit_flow_id(credit_document_id)
