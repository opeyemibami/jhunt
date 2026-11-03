"""Live run status (ADR 0017, owner's rule): anything that queues a run
updates in place, without a page reload: queued, then running, then the
result or the failure reason.

A page puts a status element where the run's state goes (status_element),
and the polling script once (SCRIPT). The script asks GET /runs/<id> every
2 seconds and replaces only that element's text until the run is done. If
the script doesn't run, the <noscript> refresh (NOSCRIPT_REFRESH) reloads
the page instead; a page includes it only while a run is still going. No
frontend dependency.
"""

import html
from typing import Any

from sqlalchemy import Engine, text

from jhunt.platform.runtime.registry import Registry
from jhunt.workflows import scout

ACTIVE = ("queued", "running", "interrupted")

SCRIPT = """<script>
(function () {
  function poll(el) {
    fetch("/runs/" + el.dataset.run, {headers: {"Accept": "application/json"}})
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function (run) {
        el.textContent = (el.dataset.label || "") + run.text;
        el.dataset.status = run.status;
        if (run.status === "queued" || run.status === "running") setTimeout(poll, 2000, el);
      })
      .catch(function () { setTimeout(poll, 5000, el); });
  }
  document.querySelectorAll(".live[data-run]").forEach(function (el) {
    if (el.dataset.status === "queued" || el.dataset.status === "running") poll(el);
  });
})();
</script>"""

# Reload every 5 s while something is still going, if the script didn't run.
NOSCRIPT_REFRESH = '<noscript><meta http-equiv="refresh" content="5"></noscript>'


def status_element(run_id: int, status: str, shown: str, *, label: str = "") -> str:
    """The element the script updates: label + the run's state."""
    e = html.escape
    return (
        f'<span class="live" data-run="{run_id}" data-status="{e(status)}"'
        f' data-label="{e(label)}">{e(label + shown)}</span>'
    )


def run_status(
    engine: Engine, conninfo: str, registry: Registry, run_id: int
) -> dict[str, str] | None:
    """{status, text} for GET /runs/<id>, or None if there's no such run."""
    with engine.connect() as conn:
        run = conn.execute(
            text("SELECT workflow, thread_id, status, error FROM runs WHERE id = :i"), {"i": run_id}
        ).first()
    if run is None:
        return None
    return {"status": run.status, "text": status_text(engine, conninfo, registry, run)}


def status_text(engine: Engine, conninfo: str, registry: Registry, run: Any) -> str:
    """What a run's status element says."""
    if run.status == "failed":
        return f"failed: {run.error or 'no reason recorded'}"
    if run.status != "succeeded":
        return str(run.status)
    if run.workflow == scout.WORKFLOW:
        lines = scout.run_summary(conninfo, registry, run.thread_id)
        return "\n".join(lines) if lines else "succeeded"
    return "succeeded"
