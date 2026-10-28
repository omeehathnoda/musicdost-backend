"""github_safe_push.py -- shared safe-flow module (Fix 2, Phase 2.5).

STAGING COPY for final-gate testing only. This file lives in
~/workspace/hidden_files/phase2.5-gates/ -- NOT in any live tree, NOT
imported by any running code, and NOT deployed. Implementation installs
this exact file (byte-identical; sha256 recorded in the gates report)
into music-app/render/, then re-runs the gate suite against the
installed copy and verifies the hash matches before any caller is
wired to it.

Contract (see the Phase 2.5 plan revision 2, sections 1-4):
- Additive-only, first-wins merge. Existing GitHub values are never
  overwritten, coerced, or deleted.
- SHA-serialized PUTs with 409 refetch+remerge and bounded retries.
- Verify-before-acknowledge: a push counts only after an authoritative
  re-read confirms the expected end state.
- Version stamping: every commit message carries writer + module +
  flow versions, so mixed deployments are visible in Git history.
- Flow guard: a push aborts (zero PUTs) when recent commit history
  shows a different FLOW_VERSION.

Pure stdlib. Imports nothing from app.py, dl_engine, dtg, gh.py, or
any credential helper. Adapters (VM: gh.py api; Render: urllib +
token) implement the Transport protocol and own all I/O and auth.
"""

import re
import time

MODULE_VERSION = "1.0.0"
FLOW_VERSION = 1
EXPECTED_FLOW_VERSION = 1  # callers assert the imported module matches


# ---------------------------------------------------------------- errors
class TransportError(Exception):
    """Base for transport failures. `kind` is the stable status token."""

    kind = "transport_error"


class FetchFailed(TransportError):
    kind = "fetch_failed"


class InvalidBase(TransportError):
    kind = "invalid_base"


class Conflict409(TransportError):
    kind = "conflict"


class RateLimited(TransportError):
    kind = "rate_limited"


class ServerError(TransportError):
    kind = "server"


class AuthError(TransportError):
    kind = "auth"


class PutUncertain(TransportError):
    """PUT was sent but its outcome is unknown (timeout / dropped
    connection after send). The caller must NOT retry blindly in the
    same run; the next run resolves it idempotently via fresh fetch."""

    kind = "put_uncertain"


class CrashSim(Exception):
    """Test-only: simulates a process dying mid-operation. NOT a
    TransportError, so it propagates through safe_push untouched --
    exactly like a real crash would."""


# ------------------------------------------------------------ versioning
_MARKER_RE = re.compile(r"\bflow=(\d+)\b")


def version():
    """Exact version identity of the running module."""
    return {"module": MODULE_VERSION, "flow": FLOW_VERSION}


def check_flow_version(flow):
    """Caller startup assertion: refuse to use a module whose flow
    semantics differ from what the caller was built against."""
    if flow != EXPECTED_FLOW_VERSION:
        raise ValueError(
            "incompatible flow version: got %r, expected %r"
            % (flow, EXPECTED_FLOW_VERSION)
        )


def parse_flow_markers(messages):
    """Extract flow versions from commit messages. Messages without a
    marker (all pre-safe-push pushes) yield nothing -- they are
    'unmarked history', allowed as the transition baseline."""
    out = []
    for m in messages or []:
        match = _MARKER_RE.search(m or "")
        if match:
            out.append(int(match.group(1)))
    return out


def commit_message(writer_id, added, conflicts):
    """Fixed format -- parseable by parse_flow_markers. Deliberately
    NOT free-form: the flow guard depends on this exact shape."""
    return (
        "safe-push writer=%s module=%s flow=%d added=%d conflicts=%d"
        % (writer_id, MODULE_VERSION, FLOW_VERSION, len(added), len(conflicts))
    )


# ----------------------------------------------------------------- merge
def _validate_bind(key, value):
    """A bind is valid iff it carries a non-empty file_id. Raises
    ValueError (programmer error, fail fast, zero network) -- never
    silently persisted, never coerced."""
    if isinstance(value, str):
        if not value:
            raise ValueError("invalid bind %r: empty file_id" % (key,))
    elif isinstance(value, dict):
        fid = value.get("file_id")
        if not isinstance(fid, str) or not fid:
            raise ValueError("invalid bind %r: dict without file_id" % (key,))
    else:
        raise ValueError("invalid bind %r: unsupported value type" % (key,))


def merge_additive(base, new_binds):
    """Pure function, no I/O.

    Returns (merged, added, already_present, conflicts).
    - absent -> add, origin format preserved (bare stays bare, dict
      stays dict);
    - present with equal value -> already_present (no PUT for it);
    - present with different value -> conflict: base value kept, key
      reported, never overwritten, never coerced, never deleted.
    """
    merged = dict(base)
    added, already, conflicts = [], [], []
    for key, value in new_binds.items():
        _validate_bind(key, value)
        if key not in merged:
            merged[key] = value
            added.append(key)
        elif merged[key] == value:
            already.append(key)
        else:
            conflicts.append(key)
    return merged, added, already, conflicts


# ------------------------------------------------------------- transport
class Transport:
    """Duck-typed protocol. Adapters subclass this; the module never
    touches the network, credentials, or the filesystem directly."""

    def get_contents(self):
        """Return (content_dict, sha). Raise FetchFailed/InvalidBase."""
        raise NotImplementedError

    def put_contents(self, content, sha, message):
        """PUT whole file with sha. Return the new sha. Raise
        Conflict409 / RateLimited / ServerError / AuthError /
        PutUncertain."""
        raise NotImplementedError

    def get_verify(self):
        """Authoritative re-read for post-push verify (API, never
        raw/CDN). Raise FetchFailed on read failure."""
        raise NotImplementedError

    def get_recent_flow_markers(self):
        """Flow versions parsed from recent tg_cache commit messages
        (adapter-owned window, e.g. last 10). Empty list = unmarked
        history (all pre-safe-push pushes)."""
        raise NotImplementedError


# -------------------------------------------------------------- safe_push
def _verify(live, expected, pre_state):
    """End-state check: every expected bind present with its exact
    value, and every pre-existing key unchanged."""
    if not isinstance(live, dict):
        return False
    for key, value in expected.items():
        if live.get(key) != value:
            return False
    for key, value in pre_state.items():
        if live.get(key) != value:
            return False
    return True


def safe_push(transport, new_binds, *, writer_id, max_attempts=3,
              sleeper=None, flow_guard=True):
    """Push new_binds to GitHub tg_cache.json under the safe flow.

    Returns a result dict:
      status: pushed | noop | failed | uncertain
        - pushed: PUT landed AND post-push verify passed.
        - noop: nothing to add (all keys already present or in
          conflict); zero PUTs.
        - failed: nothing acknowledged; binds retained by caller.
          error kind: fetch_failed | invalid_base | flow_mismatch |
          auth | server | verify_failed | conflict_exhausted |
          rate_exhausted | transport_error.
        - uncertain: PUT outcome or verify read could not be
          established; caller retains pending; the NEXT run resolves
          it idempotently (never a blind re-PUT in this run).
      added / already_present / conflicts: per-key outcomes.
      attempts: PUT attempts made. pre_sha / post_sha: SHAs bracketing
        the push (post_sha is the PUT's returned sha, even on
        verify_failed, so the record shows what landed).
      module_version / flow_version: version stamp of this run.
      warnings: non-fatal notes (e.g. flow_guard_unavailable).
    """
    if sleeper is None:
        sleeper = time.sleep
    result = {
        "status": None,
        "added": [],
        "already_present": [],
        "conflicts": [],
        "attempts": 0,
        "pre_sha": None,
        "post_sha": None,
        "error": None,
        "module_version": MODULE_VERSION,
        "flow_version": FLOW_VERSION,
        "warnings": [],
    }
    # Validate before any network: invalid binds are programmer error.
    for key, value in new_binds.items():
        _validate_bind(key, value)

    try:
        base, sha = transport.get_contents()
    except InvalidBase:
        result.update(status="failed", error="invalid_base")
        return result
    except TransportError:
        result.update(status="failed", error="fetch_failed")
        return result
    if not isinstance(base, dict):
        result.update(status="failed", error="invalid_base")
        return result
    result["pre_sha"] = sha

    if flow_guard:
        try:
            markers = transport.get_recent_flow_markers()
        except TransportError:
            markers = None
            result["warnings"].append("flow_guard_unavailable")
        if markers:
            foreign = [m for m in markers if m != FLOW_VERSION]
            if foreign:
                result.update(status="failed", error="flow_mismatch")
                return result

    merged, added, already, conflicts = merge_additive(base, new_binds)
    result.update(added=added, already_present=already, conflicts=conflicts)
    if not added:
        result.update(status="noop", post_sha=sha)
        return result
    expected = {k: v for k, v in new_binds.items() if k not in conflicts}

    last_error = None
    for attempt in range(1, max_attempts + 1):
        result["attempts"] = attempt
        msg = commit_message(writer_id, added, conflicts)
        try:
            new_sha = transport.put_contents(merged, sha, msg)
        except Conflict409:
            last_error = "conflict"
            try:
                base, sha = transport.get_contents()
            except TransportError:
                result.update(status="failed", error="fetch_failed")
                return result
            if not isinstance(base, dict):
                result.update(status="failed", error="invalid_base")
                return result
            result["pre_sha"] = sha
            merged, added, already, conflicts = merge_additive(base, new_binds)
            result.update(added=added, already_present=already,
                          conflicts=conflicts)
            expected = {k: v for k, v in new_binds.items()
                        if k not in conflicts}
            if not added:
                result.update(status="noop", post_sha=sha)
                return result
            continue
        except RateLimited:
            last_error = "rate_limited"
            if attempt < max_attempts:
                sleeper(1.0 * (2 ** (attempt - 1)))
            continue
        except AuthError:
            result.update(status="failed", error="auth")
            return result
        except ServerError:
            result.update(status="failed", error="server")
            return result
        except PutUncertain:
            result.update(status="uncertain", error="put_uncertain")
            return result
        except TransportError:
            result.update(status="failed", error="transport_error")
            return result

        # PUT confirmed by the transport: verify by authoritative re-read.
        try:
            live = transport.get_verify()
        except TransportError:
            # The PUT landed (transport confirmed it); only the verify
            # READ failed. Uncertain, not failed -- the next run's
            # fresh fetch resolves it (present -> noop -> cleared).
            result.update(status="uncertain", error="verify_unavailable",
                          post_sha=new_sha)
            return result
        if not _verify(live, expected, base):
            result.update(status="failed", error="verify_failed",
                          post_sha=new_sha)
            return result
        result.update(status="pushed", post_sha=new_sha)
        return result

    if last_error == "conflict":
        result.update(status="failed", error="conflict_exhausted")
    elif last_error == "rate_limited":
        result.update(status="failed", error="rate_exhausted")
    else:
        result.update(status="failed", error="exhausted")
    return result
