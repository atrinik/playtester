"""The feedback filer: files the owner's approved items as GitHub issues.

docs/LEARNING_AND_FEEDBACK.md 4.10.6, phase F6 ("F6 as built" there records
how this differs from the design). ``atrinik-playtester feedback file`` runs
this module in its own process, started by its own systemd unit (a oneshot
on a timer) or by hand. The game process never imports it: it is the only
code that reads the GitHub App's private key or holds a token.

For each ``approved`` item, oldest decision first:

1. It must not be withheld, must have been screened by the running bot with
   its full private terms since its text last changed and since the owner
   approved it, and must pass this process's own screen (the terms in its
   configuration and every chat sender in her memory). Otherwise it is
   skipped (and withheld, when a term matches).
2. ``checking``: the remote duplicate check (4.10.2) with the target
   repository's installation token; a fingerprint outside a code block, or
   the subject's map path or name in a title, sets ``needs-review`` with
   the candidates, unless the owner chose to file anyway.
3. ``filing``: the issue (or, for a ``comment:`` target, the comment) is
   created, the issue read back, and the item set ``filed``, or
   ``filed-unlabelled`` when a label or the type is missing from it.

Every request is one ``feedback_filings`` row: method, path, repository,
status, the issue's URL, an error and when GitHub said to come back; never
a query, a header, a key, a JWT or a token. Neither the key, the JWT nor a
token is ever logged, printed, stored or put in an error message.

Without the App key nothing is sent: approved items stay approved and their
Markdown copies are printed for the owner to file by hand (the dry run).
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import re
import signal
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from . import feedback

API = "https://api.github.com"
API_VERSION = "2022-11-28"
USER_AGENT = "atrinik-playtester-feedback-filer"
# The App key as a systemd credential of the filer's unit (LoadCredential=).
KEY_CREDENTIAL = "feedback-app.pem"
KEY_BYTES = 64 * 1024
# Limits (4.10.6): at most this many issues or comments created an hour and
# a day, at least this far apart; no request while GitHub's remaining quota
# is under RATE_FLOOR; a 403 or 429 backs off from BACKOFF_START, doubling.
HOURLY_FILINGS = 5
DAILY_FILINGS = 20
FILING_SPACING = 60.0
RATE_FLOOR = 50
BACKOFF_START = 600.0
BACKOFF_MAX = 24 * 3600.0
JWT_SECONDS = 540
REQUEST_TIMEOUT = 30.0
RESPONSE_LIMIT = 4 * 1024 * 1024
PAGES = 10
ERROR_CHARS = 300
RESPONSE_MESSAGE_CHARS = 4096

Send = Callable[[str, str, dict[str, str], bytes | None], tuple[int, dict[str, str], bytes]]


class FilingError(RuntimeError):
    """A request failed; the message holds no credential."""


class Throttled(FilingError):
    """GitHub asked the filer to wait, or a local limit was reached."""

    def __init__(self, message: str, retry_at: float):
        super().__init__(message)
        self.retry_at = retry_at


@dataclass(slots=True)
class Response:
    status: int
    headers: dict[str, str]
    data: Any


@dataclass(slots=True)
class Session:
    """One repository's installation token and labels, for this run only
    (the token is never in its repr)."""
    token: str = field(repr=False)
    labels: dict[str, str] = field(default_factory=dict)


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: urllib would send the Authorization header
    to whatever ``Location`` says. A 3xx is returned as the answer."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


_OPENER = urllib.request.build_opener(_NoRedirects)


def urllib_send(method: str, url: str, headers: dict[str, str],
                body: bytes | None) -> tuple[int, dict[str, str], bytes]:
    """One HTTPS request to the GitHub API with the standard library; a
    redirect is never followed."""
    if not url.startswith(API + "/"):
        raise FilingError("refusing a request outside the GitHub API")
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with _OPENER.open(request, timeout=REQUEST_TIMEOUT) as response:
            return (response.status, {k.casefold(): v for k, v in response.headers.items()},
                    response.read(RESPONSE_LIMIT))
    except urllib.error.HTTPError as error:
        try:
            data = error.read(RESPONSE_LIMIT) if error.fp is not None else b""
        except (OSError, http.client.HTTPException):
            data = b""
        finally:
            error.close()
        return error.code, {k.casefold(): v for k, v in (error.headers or {}).items()}, data
    except http.client.HTTPException as error:
        raise FilingError(f"cannot reach GitHub: {error.__class__.__name__}") from None
    except (urllib.error.URLError, OSError) as error:
        # The reason only: never the request, which carries the credential.
        reason = getattr(error, "reason", None) or error.__class__.__name__
        raise FilingError(f"cannot reach GitHub: {str(reason)[:ERROR_CHARS]}") from None


def key_path(explicit: Path | None) -> Path | None:
    """The App key: ``--app-key-file``, else the filer unit's systemd
    credential ``feedback-app.pem``; None when neither is set."""
    if explicit:
        return Path(explicit)
    credentials = os.getenv("CREDENTIALS_DIRECTORY", "")
    return Path(credentials) / KEY_CREDENTIAL if credentials else None


def load_app_key(path: Path) -> Any:
    """The App's RSA private key. The file must be owned by this user or
    root and readable by nobody else. Errors never name the path or show
    its contents."""
    from cryptography.exceptions import UnsupportedAlgorithm
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    try:
        with open(path, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise FilingError("the App key is not a regular file")
            data = handle.read(KEY_BYTES)
    except OSError as error:
        raise FilingError(f"cannot read the App key: {error.strerror or 'error'}") from None
    if info.st_mode & 0o077 or info.st_uid not in (os.getuid(), 0):
        raise FilingError("the App key file must be owned by this user (or root) "
                          "and readable by nobody else (mode 0400 or 0600)")
    try:
        key = serialization.load_pem_private_key(data, password=None)
    except (ValueError, TypeError, UnsupportedAlgorithm):
        raise FilingError("the App key is not an unencrypted PEM private key") from None
    finally:
        del data
    if not isinstance(key, rsa.RSAPrivateKey):
        raise FilingError("the App key is not an RSA key")
    return key


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def app_jwt(key: Any, app_id: str, now: float) -> str:
    """A short-lived App JWT (RS256), as GitHub documents: issued a minute
    in the past for clock drift, valid for ``JWT_SECONDS``."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    issuer: Any = int(app_id) if app_id.isdigit() else app_id
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"},
                                separators=(",", ":")).encode())
    claims = _b64url(json.dumps({"iat": int(now) - 60, "exp": int(now) + JWT_SECONDS,
                                 "iss": issuer}, separators=(",", ":")).encode())
    signing_input = f"{header}.{claims}".encode("ascii")
    signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{claims}.{_b64url(signature)}"


def issue_url_pattern(repo: str, *, comment: bool = False) -> re.Pattern:
    suffix = r"(?:#issuecomment-\d+)?" if comment else ""
    return re.compile(rf"^https://github\.com/{re.escape(repo)}/issues/(\d+){suffix}$")


def comment_target(item: dict[str, Any]) -> int | None:
    """The issue number of a ``comment:<url>`` target in the item's own
    repository, None for a new issue; a malformed target raises."""
    target = str(item.get("target") or "new-issue")
    if target == "new-issue":
        return None
    match = issue_url_pattern(str(item["repo"])).match(target.removeprefix("comment:"))
    if not target.startswith("comment:") or match is None:
        raise FilingError("its target is neither a new issue nor an issue of its repository")
    return int(match.group(1))


def _message(response: Response) -> str:
    data = response.data
    message = data.get("message", "") if isinstance(data, dict) else ""
    return " ".join(str(message).split())[:RESPONSE_MESSAGE_CHARS]


def _dict(data: Any) -> dict[str, Any]:
    """A JSON answer as a dict ({} for anything else)."""
    return data if isinstance(data, dict) else {}


# Anything shaped like a JWT or a GitHub token, whoever's.
# No word boundary before the prefixes: "token_ghs_..." must match too.
_CREDENTIAL_SHAPES = re.compile(
    r"eyJ[\w-]*(?:\.[\w-]*){0,2}|gh[a-z]_\w+|github_pat_\w+"
    r"|(?<![A-Za-z0-9])v1\.[0-9a-f]{40}(?![0-9a-f])")


def _block_sigterm() -> bool:
    """Hold SIGTERM back (it stays pending) while the run cleans up."""
    try:
        signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
        return True
    except (AttributeError, ValueError, OSError):
        return False


def _unblock_sigterm(blocked: bool) -> None:
    if blocked:
        signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM})


def _redact(text: str, auth: str) -> str:
    """``text`` with the request's credential, and anything shaped like a
    JWT or a token, removed, in case an answer echoes one. Done before any
    truncation, so no fragment survives."""
    for secret in {auth, auth.removeprefix("Bearer ")}:
        if len(secret) >= 8:
            text = text.replace(secret, "[redacted]")
    return _CREDENTIAL_SHAPES.sub("[redacted]", text)


class GitHub:
    """GitHub's REST API for the filer: every request audited, rate limits
    and back-off read from (and kept in) ``feedback_filings``."""

    def __init__(self, memory: Any, send: Send, *, clock: Callable[[], float],
                 sleep: Callable[[float], None]):
        self.memory, self.send, self.clock, self.sleep = memory, send, clock, sleep
        self.stop_at = 0.0
        self.stop_reason = ""

    # -- local limits from the audit -----------------------------------
    def wait_until(self) -> tuple[float, str]:
        """When the filer may send again, and why it may not now."""
        now = self.clock()
        rows = self.memory.feedback_filings(since=now - 86400.0)
        retry = max((float(row["retry_at"] or 0) for row in rows), default=0.0)
        retry = max(retry, self.stop_at)
        if retry > now:
            return retry, self.stop_reason or "GitHub asked the filer to wait"
        return 0.0, ""

    def creations(self) -> list[dict[str, Any]]:
        return [row for row in self.memory.feedback_filings(since=self.clock() - 86400.0)
                if row["request"].startswith("POST /repos/")]

    def check_caps(self) -> None:
        """Before an item is touched: the hourly and daily caps on issues
        and comments created, and any wait GitHub asked for."""
        now = self.clock()
        made = [row for row in self.creations()
                if 200 <= int(row["http_status"] or 0) < 300]
        hour = [row for row in made if float(row["at"]) >= now - 3600.0]
        if len(hour) >= HOURLY_FILINGS:
            raise Throttled(f"the hourly cap of {HOURLY_FILINGS} filings is reached",
                            float(hour[0]["at"]) + 3600.0)
        if len(made) >= DAILY_FILINGS:
            raise Throttled(f"the daily cap of {DAILY_FILINGS} filings is reached",
                            float(made[0]["at"]) + 86400.0)
        retry, reason = self.wait_until()
        if retry:
            raise Throttled(reason, retry)

    def space(self) -> None:
        """Right before an issue or comment is created: at least
        ``FILING_SPACING`` after the last attempt (waited here)."""
        rows = self.creations()
        if rows:
            wait = float(rows[-1]["at"]) + FILING_SPACING - self.clock()
            if wait > 0:
                self.sleep(wait)

    def _backoff(self, retry_after: float) -> float:
        """Exponential from ``BACKOFF_START``, doubling with each throttled
        answer since the last issue or comment created, and never before
        ``Retry-After``."""
        streak = 0
        for row in reversed(self.memory.feedback_filings(since=self.clock() - 7 * 86400.0)):
            status = int(row["http_status"] or 0)
            if 200 <= status < 300 and row["request"].startswith("POST /repos/"):
                break
            if status in (403, 429) and row["retry_at"]:
                streak += 1
        delay = min(BACKOFF_MAX, BACKOFF_START * 2 ** streak)
        return max(delay, retry_after)

    # -- requests --------------------------------------------------------
    def request(self, method: str, path: str, *, auth: str, body: Any = None,
                query: dict[str, Any] | None = None, item_id: int | None = None,
                repo: str = "", audit_url: Callable[[Any], str] | None = None,
                finishing: bool = False, cleanup: bool = False) -> Response:
        """One request; raises ``Throttled`` instead of sending while
        throttled, and ``FilingError`` on a non-2xx answer. ``finishing``
        (reading back an issue just created) is sent despite a low quota
        noticed in this run, which stops only later requests; ``cleanup``
        (revoking this run's token) is sent despite any local wait."""
        retry, reason = self.wait_until()
        if retry and not cleanup and not (finishing and retry == self.stop_at):
            raise Throttled(reason, retry)
        url = API + path + (("?" + urllib.parse.urlencode(query)) if query else "")
        headers = {"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT,
                   "X-GitHub-Api-Version": API_VERSION, "Authorization": auth}
        payload = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        at = self.clock()
        row: dict[str, Any] = {"item_id": item_id, "at": at, "request": f"{method} {path}",
                               "repo": repo}
        try:
            status, response_headers, raw = self.send(method, url, headers, payload)
        except FilingError as error:
            row["error"] = _redact(str(error), auth)[:ERROR_CHARS]
            self.memory.record_feedback_filing(row)
            raise FilingError(row["error"]) from None
        except Exception as error:
            # Anything else from the transport: its type only.
            row["error"] = f"cannot reach GitHub: {error.__class__.__name__}"
            self.memory.record_feedback_filing(row)
            raise FilingError(row["error"]) from None
        finally:
            del headers
        try:
            data = json.loads(raw or b"null")
        except ValueError:
            data = None
        response = Response(status, {k.casefold(): v for k, v in response_headers.items()},
                            data)
        row["http_status"] = status
        retry_after = self._retry_after(response, at)
        remaining = response.headers.get("x-ratelimit-remaining", "")
        message = _redact(_message(response), auth)
        throttled = status == 429 or (status == 403 and (
            "retry-after" in response.headers or remaining == "0" or
            "rate limit" in message.casefold()))
        # The floor is for the core quota: the search quota is 30 a minute,
        # so its remaining count is always under it.
        resource = response.headers.get("x-ratelimit-resource", "").strip().casefold()
        limit = response.headers.get("x-ratelimit-limit", "")
        core = resource == "core" or (not resource and not (limit.isdigit() and
                                                            int(limit) < 1000))
        if throttled:
            row["retry_at"] = at + self._backoff(retry_after)
            row["error"] = (f"throttled by GitHub ({status}): " + message)[:ERROR_CHARS]
        elif core and remaining.isdigit() and int(remaining) < RATE_FLOOR:
            reset = response.headers.get("x-ratelimit-reset", "")
            self.stop_at = float(reset) if reset.isdigit() else at + BACKOFF_START
            self.stop_reason = f"GitHub's remaining request quota is under {RATE_FLOOR}"
            row["retry_at"] = self.stop_at
        elif not 200 <= status < 300:
            row["error"] = (f"GitHub answered {status}: " + message)[:ERROR_CHARS]
        if 200 <= status < 300 and audit_url is not None:
            row["issue_url"] = audit_url(data)
        self.memory.record_feedback_filing(row)
        if throttled:
            raise Throttled(row["error"], row["retry_at"])
        if not 200 <= status < 300:
            raise FilingError(row["error"])
        return response

    @staticmethod
    def _retry_after(response: Response, at: float) -> float:
        value = response.headers.get("retry-after", "").strip()
        if value.isdigit():
            return float(value)
        reset = response.headers.get("x-ratelimit-reset", "")
        if response.headers.get("x-ratelimit-remaining") == "0" and reset.isdigit():
            return max(0.0, float(reset) - at)
        return 0.0

    def pages(self, path: str, *, auth: str, repo: str, item_id: int | None = None,
              query: dict[str, Any] | None = None) -> list[Any]:
        """Up to ``PAGES`` pages of a list endpoint, 100 a page."""
        out: list[Any] = []
        for page in range(1, PAGES + 1):
            response = self.request("GET", path, auth=auth, repo=repo, item_id=item_id,
                                    query={**(query or {}), "per_page": 100, "page": page})
            batch = response.data if isinstance(response.data, list) else []
            out.extend(batch)
            if len(batch) < 100:
                break
        return out


class Filer:
    """One run of ``feedback file``."""

    def __init__(self, memory: Any, character: str, *, private: Iterable[str],
                 app_id: str = "", key: Any = None, send: Send | None = None,
                 clock: Callable[[], float] | None = None,
                 sleep: Callable[[float], None] | None = None,
                 out: Callable[[str], None] = print):
        self.memory, self.character = memory, character
        self.private = list(private)
        self.app_id = app_id
        self._key = key
        clock = clock or time.time
        self.github = GitHub(memory, send or urllib_send, clock=clock,
                             sleep=sleep or time.sleep)
        self.clock = clock
        self.out = out
        self._sessions: dict[str, Session] = {}
        self.failures = 0

    # -- the queue -------------------------------------------------------
    def recover(self) -> None:
        """Items a crashed run left behind: ``checking`` goes back to
        ``approved`` (nothing was created). ``filing`` whose create the audit
        shows succeeded (or whose URL was kept) is filed, unverified
        (``filed-unlabelled`` for an issue: its labels and type were never
        read back); any other ``filing`` becomes ``failed``, since GitHub
        may have created it."""
        for item in self.memory.feedback_items(self.character, status="checking", limit=1000):
            self.memory.update_feedback(self.character, item["id"], expect_status=("checking",),
                                        fields={"status": "approved"})
        for item in self.memory.feedback_items(self.character, status="filing", limit=1000):
            url = item.get("issue_url") or ""
            if not url:
                created = [row for row in self.memory.feedback_filings(item_id=item["id"])
                           if row["request"].startswith("POST /repos/") and
                           200 <= int(row["http_status"] or 0) < 300 and row["issue_url"]]
                url = created[-1]["issue_url"] if created else ""
            if url:
                comment = "#issuecomment-" in url
                self.memory.update_feedback(
                    self.character, item["id"], expect_status=("filing",),
                    fields={"status": "filed" if comment else "filed-unlabelled",
                            "issue_url": url,
                            "error": None if comment else (
                                "a filer run stopped after creating it: labels and type "
                                "were not read back")})
                continue
            self.memory.update_feedback(
                self.character, item["id"], expect_status=("filing",),
                fields={"status": "failed", "error": (
                    "a filer run stopped while filing it: check GitHub for it "
                    "before approving it again")})

    def approved(self) -> list[dict[str, Any]]:
        items = self.memory.feedback_items(self.character, status="approved", limit=1000)
        return sorted(items, key=lambda item: (float(item["decided_at"] or 0), item["id"]))

    def refusal(self, item: dict[str, Any]) -> str:
        """Why ``item`` may not be filed now, or "". A term match withholds it."""
        if item.get("withheld"):
            return "withheld: it names a private term"
        if (not feedback.readable(item) or
                float(item["screened_at"] or 0) < float(item["decided_at"] or 0)):
            return "not yet screened by the running bot since it was approved or changed"
        copy = dict(item)
        if feedback.screen([copy], self.private):
            self.memory.withhold_feedback(self.character, [item["id"]])
            return "withheld: it names a private term"
        if item["repo"] not in feedback.REPOS:
            return "its repository is not one the playtester files to"
        return ""

    def run(self, *, dry_run: bool = False, limit: int = HOURLY_FILINGS) -> int:
        """File up to ``limit`` approved items; returns an exit status."""
        self.recover()
        items = self.approved()
        ready: list[dict[str, Any]] = []
        for item in items:
            reason = self.refusal(item)
            if reason:
                self.out(f"#{item['id']} {feedback.short_fingerprint(item['fingerprint'])} "
                         f"skipped: {reason}")
            else:
                ready.append(item)
        if not ready:
            self.out("No approved item is ready to file.")
            return 0
        if dry_run or self._key is None:
            why = "dry run" if dry_run else "no App key"
            self.out(f"{why}: nothing is sent; {len(ready)} approved "
                     f"item{'s' if len(ready) != 1 else ''} stay approved. "
                     "File them by hand from these Markdown copies:")
            for item in ready:
                self.out(f"\n===== #{item['id']} "
                         f"{feedback.short_fingerprint(item['fingerprint'])} =====")
                self.out(feedback.markdown_copy(item))
            return 0
        if not self.app_id:
            self.out("ERROR set --app-id (or ATRINIK_PLAYTESTER_FEEDBACK_APP_ID)")
            return 2
        previous = self._on_sigterm()
        try:
            for item in ready[:max(0, limit)]:
                try:
                    self.file(item)
                except Throttled as error:
                    when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(error.retry_at))
                    self.out(f"stopped: {error}; trying again after {when}")
                    return 0
                except Exception as error:
                    # Never stranded mid-way, and never a request in the text.
                    self._fail(int(item["id"]), ("checking", "filing"),
                               f"internal error ({error.__class__.__name__}); nothing "
                               "more was sent for it")
        finally:
            # A SIGTERM now waits until the token is revoked and the key
            # forgotten, then reaches the handler the run started with.
            blocked = _block_sigterm()
            try:
                self.close()
            finally:
                if previous is not None:
                    signal.signal(signal.SIGTERM, previous)
                _unblock_sigterm(blocked)
        return 1 if self.failures else 0

    @staticmethod
    def _on_sigterm() -> Any:
        """Turn SIGTERM (systemd stopping the unit) into an exit through
        ``close``, so the token is revoked. None off the main thread."""
        def stop(signum: int, frame: Any) -> None:
            raise SystemExit(128 + signum)
        try:
            return signal.signal(signal.SIGTERM, stop)
        except ValueError:
            return None

    # -- one item ----------------------------------------------------------
    def _claim(self, item_id: int, expect: str, status: str, updated_at: float) -> bool:
        """Move an item on only while it is unwithheld, its text unchanged
        and screened by the running bot since its change and its approval
        (one statement: a withhold or an edit in between wins)."""
        return self.memory.update_feedback(
            self.character, item_id, expect_status=(expect,), fields={"status": status},
            updated_at=updated_at, screened_after_decision=True)

    def file(self, item: dict[str, Any]) -> None:
        item_id, short = int(item["id"]), feedback.short_fingerprint(item["fingerprint"])
        self.github.check_caps()
        if not self._claim(item_id, "approved", "checking", item["updated_at"]):
            self.out(f"#{item_id} {short} changed, was withheld or lost its screen "
                     "meanwhile; left alone")
            return
        # Exactly the text claimed, screened again here.
        item = self.memory.feedback_item(self.character, str(item_id)) or {}
        reason = self.refusal(item) if item.get("status") == "checking" else "changed"
        if reason:
            self._set(item_id, ("checking",), status="approved")
            self.out(f"#{item_id} {short} not filed: {reason}")
            return
        if item.get("issue_url") and not item.get("file_anyway"):
            # A filing an earlier run started may exist: the owner decides.
            self._set(item_id, ("checking",), status="needs-review",
                      remote_candidates_json=json.dumps(
                          [{"url": item["issue_url"], "match": "earlier-run"}]))
            self.out(f"#{item_id} {short} needs review: an earlier run filed it as "
                     f"{item['issue_url']}")
            return
        try:
            number = comment_target(item)
            session = self.session(item["repo"])
            if not item.get("file_anyway"):
                found = self.candidates(item, session, number)
                if found:
                    self._set(item_id, ("checking",), status="needs-review",
                              remote_candidates_json=json.dumps(found),
                              error=None)
                    self.out(f"#{item_id} {short} needs review: possible duplicates "
                             + ", ".join(entry["url"] for entry in found))
                    return
            self.github.space()
        except Throttled:
            self._set(item_id, ("checking",), status="approved")
            raise
        except FilingError as error:
            self._fail(item_id, ("checking",), str(error))
            return
        if not self._claim(item_id, "checking", "filing", item["updated_at"]):
            self._set(item_id, ("checking",), status="approved")
            self.out(f"#{item_id} {short} was withheld or changed while it was checked; "
                     "not filed")
            return
        try:
            if number is None:
                self.create_issue(item, session)
            else:
                self.create_comment(item, session, number)
        except Throttled:
            # The create itself was refused: nothing exists on GitHub.
            self._set(item_id, ("filing",), status="approved")
            raise
        except FilingError as error:
            self._fail(item_id, ("filing",), str(error))

    def _set(self, item_id: int, expect: tuple[str, ...], **fields: Any) -> bool:
        return self.memory.update_feedback(self.character, item_id, expect_status=expect,
                                           fields=fields)

    def _fail(self, item_id: int, expect: tuple[str, ...], error: str) -> None:
        self.failures += 1
        self._set(item_id, expect, status="failed", error=error[:ERROR_CHARS])
        self.out(f"#{item_id} failed: {error[:ERROR_CHARS]}")

    def session(self, repo: str) -> Session:
        """The repository's installation token (``issues: write`` for this
        one repository) and labels, made once a run."""
        if repo in self._sessions:
            return self._sessions[repo]
        owner, name = repo.split("/", 1)
        jwt = app_jwt(self._key, self.app_id, self.clock())
        try:
            installation = self.github.request(
                "GET", f"/repos/{owner}/{name}/installation", auth=f"Bearer {jwt}", repo=repo)
            installation_id = _dict(installation.data).get("id")
            if not isinstance(installation_id, int):
                raise FilingError("the App is not installed on the repository")
            minted = self.github.request(
                "POST", f"/app/installations/{installation_id}/access_tokens",
                auth=f"Bearer {jwt}", repo=repo,
                body={"repositories": [name], "permissions": {"issues": "write"}})
        finally:
            del jwt
        token = _dict(minted.data).get("token")
        if not isinstance(token, str) or not token:
            raise FilingError("GitHub returned no installation token")
        session = Session(token)
        self._sessions[repo] = session
        labels = self.github.pages(f"/repos/{owner}/{name}/labels",
                                   auth=f"Bearer {session.token}", repo=repo)
        session.labels = {str(label.get("name", "")).casefold(): str(label.get("name", ""))
                          for label in labels if isinstance(label, dict)}
        return session

    def close(self) -> None:
        """Revoke this run's tokens and forget them, also while throttled."""
        try:
            for repo, session in list(self._sessions.items()):
                try:
                    self.github.request("DELETE", "/installation/token",
                                        auth=f"Bearer {session.token}", repo=repo,
                                        cleanup=True)
                except Exception:
                    pass
        finally:
            self._sessions.clear()
            self._key = None

    def candidates(self, item: dict[str, Any], session: Session,
                   number: int | None) -> list[dict[str, str]]:
        """Existing issues (or comments) that may already report this item
        (4.10.2): its fingerprint outside a code block, or for a new issue
        the subject's map path or name in a title."""
        repo, item_id = str(item["repo"]), int(item["id"])
        short = feedback.short_fingerprint(item["fingerprint"])
        auth = f"Bearer {session.token}"
        found: dict[str, str] = {}
        if number is not None:
            comments = self.github.pages(f"/repos/{repo}/issues/{number}/comments",
                                         auth=auth, repo=repo, item_id=item_id)
            pattern = issue_url_pattern(repo, comment=True)
            for comment in comments:
                if not isinstance(comment, dict):
                    continue
                url = str(comment.get("html_url", ""))
                if pattern.match(url) and short in feedback.fingerprints_outside_code(
                        str(comment.get("body") or "")):
                    found.setdefault(url, "fingerprint")
            return [{"url": url, "match": match} for url, match in found.items()]
        pattern = issue_url_pattern(repo)
        searches = [(f'repo:{repo} is:issue "{short}"', "fingerprint", short)]
        searches += [(f'repo:{repo} is:issue in:title "{term}"', "title", term)
                     for term in feedback.search_terms(str(item["subject"]))]
        for query, match, needle in searches:
            response = self.github.request("GET", "/search/issues", auth=auth, repo=repo,
                                           item_id=item_id,
                                           query={"q": query, "per_page": 30})
            results = _dict(response.data).get("items", [])
            results = results if isinstance(results, list) else []
            for issue in results:
                if not isinstance(issue, dict) or "pull_request" in issue:
                    continue
                url = str(issue.get("html_url", ""))
                if not pattern.match(url):
                    continue
                if match == "fingerprint":
                    hit = short in feedback.fingerprints_outside_code(
                        str(issue.get("body") or ""))
                else:
                    hit = needle.casefold() in str(issue.get("title") or "").casefold()
                if hit:
                    found.setdefault(url, match)
        return [{"url": url, "match": match} for url, match in found.items()]

    def create_issue(self, item: dict[str, Any], session: Session) -> None:
        item_id, repo = int(item["id"]), str(item["repo"])
        wanted = feedback.clean_labels([*feedback.labels(item), feedback.FILED_LABEL])
        sendable = [session.labels[label.casefold()] for label in wanted
                    if label.casefold() in session.labels]
        absent = [label for label in wanted if label.casefold() not in session.labels]
        kind = feedback.issue_type(item)
        payload: dict[str, Any] = {"title": item["title"], "body": item["body_md"],
                                   "labels": sendable, "type": kind}
        pattern = issue_url_pattern(repo)

        def url_of(data: Any) -> str:
            url = str((data or {}).get("html_url", "")) if isinstance(data, dict) else ""
            return url if pattern.match(url) else ""

        auth = f"Bearer {session.token}"
        created = self.github.request("POST", f"/repos/{repo}/issues", auth=auth, body=payload,
                                      repo=repo, item_id=item_id, audit_url=url_of)
        url = url_of(created.data)
        number = (created.data or {}).get("number") if isinstance(created.data, dict) else None
        if not url or not isinstance(number, int):
            raise FilingError("GitHub created an issue but returned no usable URL")
        # Kept at once: a failure from here on must not file it twice.
        self._set(item_id, ("filing",), issue_url=url)
        missing = list(absent)
        try:
            back = self.github.request("GET", f"/repos/{repo}/issues/{number}", auth=auth,
                                       repo=repo, item_id=item_id, finishing=True)
            data = _dict(back.data)
            shown_labels = data.get("labels")
            present = {str(label.get("name", "")).casefold()
                       for label in (shown_labels if isinstance(shown_labels, list) else [])
                       if isinstance(label, dict)}
            missing += [label for label in sendable if label.casefold() not in present]
            shown = data.get("type")
            type_missing = not (isinstance(shown, dict) and shown.get("name") == kind)
        except Exception:
            # Throttled, failed or malformed: it exists, unverified.
            missing, type_missing = missing + ["(not read back)"], True
        if missing or type_missing:
            parts = ([f"labels {', '.join(missing)}"] if missing else []) + (
                [f"type {kind}"] if type_missing else [])
            self._set(item_id, ("filing",), status="filed-unlabelled",
                      error=f"filed without {' and '.join(parts)}"[:ERROR_CHARS])
            self.out(f"#{item_id} filed without {' and '.join(parts)}: {url}")
        else:
            self._set(item_id, ("filing",), status="filed", error=None)
            self.out(f"#{item_id} filed: {url}")

    def create_comment(self, item: dict[str, Any], session: Session, number: int) -> None:
        item_id, repo = int(item["id"]), str(item["repo"])
        pattern = issue_url_pattern(repo, comment=True)

        def url_of(data: Any) -> str:
            url = str((data or {}).get("html_url", "")) if isinstance(data, dict) else ""
            return url if pattern.match(url) else ""

        created = self.github.request("POST", f"/repos/{repo}/issues/{number}/comments",
                                      auth=f"Bearer {session.token}",
                                      body={"body": item["body_md"]}, repo=repo,
                                      item_id=item_id, audit_url=url_of)
        url = url_of(created.data)
        if not url:
            raise FilingError("GitHub created a comment but returned no usable URL")
        self._set(item_id, ("filing",), status="filed", issue_url=url, error=None)
        self.out(f"#{item_id} filed as a comment: {url}")
