"""SQL as the signed-in user. Statement execution is skillhub.sql.SqlClient; this module maps its errors."""

from __future__ import annotations

import logging

from databricks.sdk.errors import DatabricksError, NotFound, PermissionDenied

from skillhub.sql import SqlClient
from skillhub.sql import SqlError as SkillhubSqlError

log = logging.getLogger("skillhub.app.data")


class SqlDenied(Exception):
    """The user cannot read the object. `not_found` is set when the object is missing or hidden, not refused."""

    def __init__(self, message: str = "statement refused", *, not_found: bool = False):
        super().__init__(message)
        self.not_found = not_found


class SqlError(Exception):
    """A statement failed for a reason other than access."""


# SqlClient passes on the warehouse's error message ("statement failed: <message>"). These mean "refused", not "broken".
_NOT_FOUND_MARKERS = ("TABLE_OR_VIEW_NOT_FOUND", "SCHEMA_NOT_FOUND")
_ACCESS_MARKERS = (
    "PERMISSION_DENIED",
    "INSUFFICIENT_PRIVILEGES",
    "INSUFFICIENT_PERMISSIONS",
    "USER_DOES_NOT_HAVE_PERMISSION",
)


def _marker(exc: Exception) -> str | None:
    text = str(exc).upper()
    return next((m for m in _NOT_FOUND_MARKERS + _ACCESS_MARKERS if m in text), None)


class UserSql:
    """Runs statements through a SqlClient built from the user's WorkspaceClient. It adds no SQL of its own."""

    def __init__(self, client: SqlClient):
        self._client = client

    def query(self, statement: str, params: dict[str, object] | None = None) -> list[dict[str, object]]:
        try:
            return self._client.execute(statement, params)
        except PermissionDenied as exc:
            # An API-level 403 (no CAN_USE on the warehouse, a missing scope) is a fault, not "you can't read this
            # department". Only an object-level marker in the message is a denial.
            marker = _marker(exc)
            if marker:
                self._deny(exc, marker)
            log.warning("sql call refused: PermissionDenied without an object marker")
            raise SqlError("statement refused") from None
        except NotFound as exc:
            # A 404 from the API (bad warehouse ID, say) is a fault. Only an object-level not-found is a denial.
            marker = _marker(exc)
            if marker in _NOT_FOUND_MARKERS:
                self._deny(exc, marker)
            raise SqlError("statement failed") from None
        except SkillhubSqlError as exc:
            marker = _marker(exc)
            if marker:
                self._deny(exc, marker)
            raise SqlError("statement failed") from None
        except DatabricksError as exc:
            raise SqlError(type(exc).__name__) from None

    @staticmethod
    def _deny(exc: Exception, marker: str):
        # No token, SQL or parameter values: the marker, the exception type and the first line of the server message.
        first_line = (str(exc).splitlines() or [""])[0][:200]
        log.warning("sql denied: marker=%s type=%s message=%s", marker, type(exc).__name__, first_line)
        raise SqlDenied(not_found=marker in _NOT_FOUND_MARKERS) from None
