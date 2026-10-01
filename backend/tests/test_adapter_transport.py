"""AppDaemon transport tests — PURE, no AppDaemon, no network.

Covers the two transport defects:

1. ``SceneStudioApp._serve_endpoint`` must match AppDaemon 4.5's REST model:
   ONE named endpoint (``scene_studio_api``) whose callback receives the
   decoded JSON body (POST) / query mapping (GET) and returns
   ``(json_mappable, status_code)``. The harness below replicates
   ``http.py.dispatch_app_endpoint`` + ``call_app_endpoint`` from the 4.5.0
   tag, including the behavior that 404/500 responses are replaced by HTML
   error pages (hence the adapter always answers HTTP 200 with the real
   status wrapped in the body envelope).

2. ``SceneStudioApp._bridge_call_service`` must call AppDaemon's
   ``call_service(service, **kwargs)`` with exactly ONE positional argument —
   the ``"<domain>/<service>"`` string. The fake base class below replicates
   the REAL ADAPI signature precisely; the old split-into-domain+action
   behavior would raise TypeError here.
"""

import json

import pytest

from scene_studio.appdaemon_adapter import SceneStudioApp
from scene_studio.appdaemon_adapter.adapter import ENDPOINT_NAME


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class RealSignatureAppDaemon:
    """Fake AD base replicating hassapi's call_service signature EXACTLY.

    AppDaemon 4.5 (ADAPI.call_service, 4.5.0 tag)::

        async def call_service(self, service, namespace=None, timeout=None,
                               callback=None, **data)

    The service is ONE positional string ``"<domain>/<service>"``. Any caller
    that still splits ``"light/turn_on"`` into ``call_service(domain, action)``
    fails here with ``call_service() takes ... positional arguments``.
    """

    def __init__(self):
        self.service_calls = []

    def call_service(self, service, **kwargs):
        self.service_calls.append({"service": service, **kwargs})


class FakeEngine:
    """Engine duck-type covering exactly what service/api.route() touches.

    Every read returns a sentinel tagged with the route so the transport tests
    can prove the envelope reached route() and came back through it.
    """

    def __init__(self):
        self.calls = []

    def status(self):
        self.calls.append(("status",))
        return {"engine": "sentinel-status"}

    def fixtures_catalog(self):
        self.calls.append(("fixtures",))
        return {"fixtures": ["sentinel-fixtures"]}

    def scenes_catalog(self, archived=False):
        self.calls.append(("scenes", archived))
        return {"scenes": ["sentinel-scenes"], "archived": archived}

    def latest_discovery(self):
        self.calls.append(("discovery",))
        return None  # documented empty state -> {"report": null}

    def recent_events(self, limit):
        self.calls.append(("recent", limit))
        return []

    def handle(self, envelope):
        self.calls.append(("command", envelope))
        return {"command": envelope.get("command"), "ok": True, "data": {"echo": "sentinel-command"}}

    def explode(self):  # used to force a 500 through the transport
        raise RuntimeError("engine detonated")


class ExplodingEngine(FakeEngine):
    """Engine whose route dispatch raises — verifies the never-raise guard."""

    def status(self):
        raise RuntimeError("engine detonated")


class FakeAppDaemonHttp:
    """Mimics AppDaemon 4.5 endpoint dispatch (http.py, 4.5.0 tag).

    - POST: ``args = json.loads(body)`` (AD itself answers 400 for undecodable
      JSON before the callback ever runs — mimicked in :meth:`call`).
    - GET: ``args = request.query`` (a mapping of string -> string).
    - Callback result must unpack as ``(ret, code)``; unpack errors or
      exceptions become code 500; 404/500 responses are replaced by HTML.
    """

    def __init__(self, app):
        self._callback = app._serve_endpoint

    def call(self, method, query=None, raw_body=None):
        if method == "POST":
            try:
                args = json.loads(raw_body) if raw_body else None
            except json.JSONDecodeError:
                return 400, "<html><body>400 JSON Decode Error</body></html>"
        else:
            args = dict(query or {})
        try:
            ret, code = self._callback(args)
        except TypeError:  # callback returned a non-(ret, code) value
            return 500, "<html><body>500 An Error occured</body></html>"
        except Exception:
            return 500, "<html><body>500 An Error occured</body></html>"
        if code == 404:
            return 404, "<html><body>404 App Not Found</body></html>"
        if code == 500:
            return 500, "<html><body>500 An Error occured</body></html>"
        return code, ret


def make_app(engine=None):
    """A SceneStudioApp on the import-safe stub base with a fake engine."""
    app = SceneStudioApp()
    app._engine = engine if engine is not None else FakeEngine()
    return app


# ---------------------------------------------------------------------------
# Defect 2 regression: call_service takes ONE positional service string
# ---------------------------------------------------------------------------


def test_bridge_call_service_uses_single_positional_service_string():
    fake_ad = RealSignatureAppDaemon()
    app = make_app()
    app.call_service = fake_ad.call_service  # real ADAPI signature

    app._bridge_call_service("light/turn_on", entity_id="light.lamp", brightness_pct=63)

    assert fake_ad.service_calls == [
        {"service": "light/turn_on", "entity_id": "light.lamp", "brightness_pct": 63}
    ]


def test_old_split_domain_action_behavior_would_fail_real_signature():
    """Guards the regression: the OLD bridge shape is incompatible with AD 4.5."""
    fake_ad = RealSignatureAppDaemon()
    with pytest.raises(TypeError):
        # What the old code effectively did: call_service(domain, action, **payload)
        fake_ad.call_service("light", "turn_on", entity_id="light.lamp")


def test_executor_bridge_end_to_end_over_real_signature():
    """RequestsProviderExecutor -> _bridge_call_service -> AD call_service."""
    fake_ad = RealSignatureAppDaemon()
    app = make_app()
    app.call_service = fake_ad.call_service

    app._bridge_call_service("light/turn_off", entity_id="light.desk")

    assert fake_ad.service_calls == [{"service": "light/turn_off", "entity_id": "light.desk"}]


def test_endpoint_registered_under_single_named_endpoint(tmp_path, monkeypatch):
    """initialize() must register exactly one NAMED endpoint (RPC-style)."""
    import sys
    import types

    # Satisfy the lazy `import appdaemon.plugins.hass.hassapi` inside
    # initialize() without AppDaemon installed (pure environment).
    for name in (
        "appdaemon",
        "appdaemon.plugins",
        "appdaemon.plugins.hass",
        "appdaemon.plugins.hass.hassapi",
    ):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))

    registrations = []

    class RegisteringApp(SceneStudioApp):
        def __init__(self):
            self.registrations = []

        def register_endpoint(self, callback, endpoint=None, **kwargs):
            self.registrations.append((callback, endpoint))

    app = RegisteringApp()
    app.args = {"store_root": str(tmp_path)}  # keep store construction in tmp
    app.initialize()

    assert len(app.registrations) == 1
    callback, endpoint = app.registrations[0]
    assert endpoint == ENDPOINT_NAME == "scene_studio_api"
    assert callable(callback)


# ---------------------------------------------------------------------------
# Defect 1: the RPC envelope transport (AppDaemon 4.5 REST model)
# ---------------------------------------------------------------------------


def test_post_envelope_reaches_every_route():
    engine = FakeEngine()
    ad = FakeAppDaemonHttp(make_app(engine))

    def envelope(method, path, **members):
        payload = {"method": method, "path": path, **members}
        code, ret = ad.call("POST", raw_body=json.dumps(payload))
        assert code == 200  # always HTTP 200; the real status lives in the body
        assert set(ret) == {"status", "body"}
        return ret

    assert envelope("GET", "/status") == {"status": 200, "body": {"engine": "sentinel-status"}}
    assert envelope("GET", "/fixtures") == {
        "status": 200,
        "body": {"fixtures": ["sentinel-fixtures"]},
    }
    scenes = envelope("GET", "/scenes", query={"archived": "true"})
    assert scenes == {"status": 200, "body": {"scenes": ["sentinel-scenes"], "archived": True}}
    assert envelope("GET", "/discovery") == {"status": 200, "body": {"report": None}}
    recent = envelope("GET", "/diagnostics/recent", query={"limit": "5"})
    assert recent["status"] == 200
    command = envelope("POST", "/command", body={"command": "scene.apply", "scene_id": "twilight"})
    assert command["status"] == 200
    assert command["body"]["data"] == {"echo": "sentinel-command"}

    assert ("status",) in engine.calls
    assert ("fixtures",) in engine.calls
    assert ("scenes", True) in engine.calls
    assert ("discovery",) in engine.calls
    assert ("recent", 5) in engine.calls
    assert ("command", {"command": "scene.apply", "scene_id": "twilight"}) in engine.calls


def test_get_query_fallback_reaches_route():
    """GET with ?path=... routes with the remaining params as the query."""
    engine = FakeEngine()
    ad = FakeAppDaemonHttp(make_app(engine))

    code, ret = ad.call("GET", query={"path": "/scenes", "archived": "true"})
    assert code == 200
    assert ret == {"status": 200, "body": {"scenes": ["sentinel-scenes"], "archived": True}}

    code, ret = ad.call("GET", query={"path": "/diagnostics/recent", "limit": "7"})
    assert code == 200 and ret["status"] == 200

    code, ret = ad.call("GET", query={"path": "/status"})
    assert code == 200 and ret == {"status": 200, "body": {"engine": "sentinel-status"}}


def test_route_prefix_and_relative_paths_both_resolve():
    ad = FakeAppDaemonHttp(make_app())
    for path in ("/status", "/api/scene_studio/status", "status"):
        code, ret = ad.call("POST", raw_body=json.dumps({"method": "GET", "path": path}))
        assert code == 200 and ret == {"status": 200, "body": {"engine": "sentinel-status"}}, path


def test_unknown_path_returns_404_inside_200_body_envelope():
    """404 can never ride the HTTP status: AD would swap the body for HTML."""
    ad = FakeAppDaemonHttp(make_app())
    code, ret = ad.call("POST", raw_body=json.dumps({"method": "GET", "path": "/nope"}))

    assert code == 200  # not 404 — the envelope carries it instead
    assert ret["status"] == 404
    assert ret["body"]["ok"] is False
    assert ret["body"]["error"]["code"] == "not_found"


def test_wrong_method_for_known_path_is_a_404_envelope():
    ad = FakeAppDaemonHttp(make_app())
    code, ret = ad.call("POST", raw_body=json.dumps({"method": "POST", "path": "/status"}))
    assert code == 200 and ret["status"] == 404
    code, ret = ad.call("GET", query={"path": "/command"})
    assert code == 200 and ret["status"] == 404


def test_command_without_body_gets_route_400_envelope():
    ad = FakeAppDaemonHttp(make_app())
    code, ret = ad.call("POST", raw_body=json.dumps({"method": "POST", "path": "/command"}))
    assert code == 200
    assert ret["status"] == 400
    assert ret["body"]["error"]["code"] == "validation_error"


@pytest.mark.parametrize(
    "raw_body",
    [
        json.dumps({}),  # no path
        json.dumps({"method": "GET"}),  # no path
        json.dumps({"path": 42}),  # non-string path
        json.dumps({"path": "/status", "method": "DELETE"}),  # unsupported method
        json.dumps([1, 2, 3]),  # JSON array, not an object envelope
        json.dumps("just a string"),  # JSON scalar
    ],
)
def test_malformed_envelopes_return_400_style_body(raw_body):
    ad = FakeAppDaemonHttp(make_app())
    code, ret = ad.call("POST", raw_body=raw_body)
    assert code == 200
    assert ret["status"] == 400
    assert ret["body"]["ok"] is False
    assert ret["body"]["error"]["code"] == "validation_error"


def test_callback_exception_becomes_500_body_never_raises_out():
    ad = FakeAppDaemonHttp(make_app(ExplodingEngine()))
    code, ret = ad.call("POST", raw_body=json.dumps({"method": "GET", "path": "/status"}))
    assert code == 200  # exception stayed inside the callback
    assert ret["status"] == 500
    assert ret["body"]["ok"] is False
    assert ret["body"]["error"]["code"] == "internal_error"
    assert "engine detonated" in ret["body"]["error"]["message"]


def test_non_mapping_get_args_are_rejected_not_raised():
    """POST body that decodes to a non-object cannot crash the callback."""
    ad = FakeAppDaemonHttp(make_app())
    code, ret = ad.call("POST", raw_body="null")
    assert code == 200 and ret["status"] == 400

    code, ret = ad.call("POST", raw_body=None)  # empty body -> args None
    assert code == 200 and ret["status"] == 400


def test_callback_tolerates_ad_kwargs_and_extra_positional_rargs():
    """AD may invoke the callback as cb(args, rargs) or cb(args, request=...)."""
    app = make_app()
    # sync dispatch without expanded kwargs passes rargs positionally
    ret, code = app._serve_endpoint({"method": "GET", "path": "/status"}, {"request": "obj"})
    assert code == 200 and ret["body"] == {"engine": "sentinel-status"}
    # expanded-kwargs dispatch passes the request object as a kwarg
    ret, code = app._serve_endpoint({"method": "GET", "path": "/status"}, request="obj")
    assert code == 200 and ret["body"] == {"engine": "sentinel-status"}


def test_response_body_survives_where_ad_would_emit_html():
    """End-to-end shape check: every routed outcome reaches the caller as JSON."""
    ad = FakeAppDaemonHttp(make_app())
    for path in ("/status", "/fixtures", "/scenes", "/discovery", "/diagnostics/recent"):
        code, ret = ad.call("POST", raw_body=json.dumps({"method": "GET", "path": path}))
        assert code == 200 and isinstance(ret, dict) and "status" in ret and "body" in ret, path
