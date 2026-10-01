"""AppDaemon adapter tests — PURE parts only, no network, no AppDaemon.

The module must import without appdaemon installed; the executor is tested
with a fake ``requests`` module (monkeypatched/injected) covering URL and
payload building, receipt shapes, credential guards, and failure mapping,
plus legacy-envelope parsing through the frozen catalog.
"""

import pytest

from scene_studio.appdaemon_adapter import HAS_APPDAEMON, RequestsProviderExecutor, SceneStudioApp
from scene_studio.domain.commands import parse_command
from scene_studio.domain.fidelity import ProviderOperation
from scene_studio.domain.fixtures import Fixture
from scene_studio.service import legacy


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code=200, text="", json_body=None):
        self.status_code = status_code
        self.text = text
        self._json_body = json_body

    def json(self):
        if self._json_body is None:
            raise ValueError("not JSON")  # mirrors requests' behavior for a non-JSON body
        return self._json_body


class FakeRequests:
    """Stand-in for the ``requests`` module; records every call."""

    def __init__(
        self,
        put_status=200,
        post_status=200,
        raise_exc=None,
        put_json_body=None,
        put_json_bodies=None,
        put_statuses=None,
        get_json_bodies=None,
        get_status=200,
    ):
        self.put_calls = []
        self.post_calls = []
        self.get_calls = []
        self._put_status = put_status
        self._post_status = post_status
        self._raise_exc = raise_exc
        self._put_json_body = put_json_body
        # Successive PUT responses (retry escalation issues PUT more than
        # once); the last one repeats once the list is exhausted.
        self._put_json_bodies = list(put_json_bodies) if put_json_bodies is not None else None
        self._put_statuses = list(put_statuses) if put_statuses is not None else None
        # Successive GET responses (read-back confirmation retries call GET
        # more than once); the last one repeats once the list is exhausted.
        self._get_json_bodies = list(get_json_bodies) if get_json_bodies is not None else None
        self._get_status = get_status

    def put(self, url, **kwargs):
        if self._raise_exc is not None:
            raise self._raise_exc
        self.put_calls.append({"url": url, **kwargs})
        status = self._put_status
        if self._put_statuses is not None:
            status = self._put_statuses.pop(0) if len(self._put_statuses) > 1 else self._put_statuses[0]
        if self._put_json_bodies is not None:
            body = self._put_json_bodies.pop(0) if len(self._put_json_bodies) > 1 else self._put_json_bodies[0]
        else:
            body = self._put_json_body
        return FakeResponse(status, "body", json_body=body)

    def post(self, url, **kwargs):
        if self._raise_exc is not None:
            raise self._raise_exc
        self.post_calls.append({"url": url, **kwargs})
        return FakeResponse(self._post_status, "body")

    def get(self, url, **kwargs):
        self.get_calls.append({"url": url, **kwargs})
        if self._get_json_bodies is None:
            return FakeResponse(self._get_status, "body")
        body = self._get_json_bodies.pop(0) if len(self._get_json_bodies) > 1 else self._get_json_bodies[0]
        return FakeResponse(self._get_status, "body", json_body=body)


def make_fixture(fixture_id="lamp_fixture"):
    return Fixture.from_dict(
        {
            "id": fixture_id,
            "name": "Lamp Fixture",
            "binding": {"provider": "ha_light", "ha_entity_id": "light.lamp"},
        }
    )


def make_operation(provider, op, resource_ref, payload=None):
    return ProviderOperation(provider=provider, op=op, resource_ref=resource_ref, payload=payload or {})


def make_hue_fixture(fixture_id="gledopto_strip", dynamic_native=False):
    return Fixture.from_dict(
        {
            "id": fixture_id,
            "name": "GLEDOPTO Strip",
            "binding": {"provider": "hue_v2", "bridge_id": "bridge1", "resource_id": "rid-123"},
            "capabilities": {"on_off": True, "brightness": True, "color_xy": True, "dynamic_native": dynamic_native},
        }
    )


# ---------------------------------------------------------------------------
# import safety without AppDaemon
# ---------------------------------------------------------------------------


def test_module_imports_without_appdaemon():
    assert HAS_APPDAEMON is False  # dev/test environment has no appdaemon
    assert SceneStudioApp is not None
    assert RequestsProviderExecutor is not None


def test_legacy_envelopes_parse_against_frozen_catalog():
    # the adapter hands mapper output straight to engine.handle(); the
    # envelopes must parse against COMMAND_CATALOG unchanged.
    parsed = parse_command(legacy.map_apply_scene_event({"scene_name": "Twilight Mode"}))
    assert parsed[0].command == "scene.apply"
    assert parsed[1].scene_id == "twilight_mode"

    parsed = parse_command(legacy.map_save_light_states_event({"scene_name": "Movie Night"}))
    assert parsed[0].command == "scene.save"
    assert parsed[1].name == "Movie Night"

    parsed = parse_command(legacy.map_delete_scene_event({"scene_name": "Old Scene"}))
    assert parsed[0].command == "scene.archive"
    assert parsed[1].scene_id == "old_scene"

    generate = legacy.map_generate_office_scene({"colors": ["#ff0000", "#0000ff"]})
    save_envelope, apply_envelope = generate["commands"]
    assert parse_command(save_envelope)[0].command == "scene.save"
    assert parse_command(apply_envelope)[0].command == "scene.apply"


# ---------------------------------------------------------------------------
# RequestsProviderExecutor
# ---------------------------------------------------------------------------


def test_http_session_is_created_once_and_reused_across_calls():
    """Live finding: identical `fixture.identify` calls against a real Hue
    bridge alternated ~0.2s/~1.0s — a fresh TLS handshake every other call,
    because the untouched executor used module-level requests.put() (which
    spins up and tears down its own throwaway Session per call) instead of
    one persistent Session. This locks in the fix: _http() must hand back
    the SAME object every time so urllib3 actually pools the connection."""
    executor = RequestsProviderExecutor(hue_ip="192.0.2.10", hue_username="example-hue-appkey")  # secrets-scan:allow (placeholder)
    first = executor._http()
    second = executor._http()
    assert first is second
    import requests

    assert isinstance(first, requests.Session)


def test_hue_put_builds_clip_v2_request_with_key_header():
    fake = FakeRequests()
    executor = RequestsProviderExecutor(
        hue_ip="192.0.2.10",
        hue_username="example-hue-appkey",  # placeholder value, test-only
        requests_module=fake,
    )
    operation = make_operation("hue_v2", "hue.put_light", "rid-123", {"on": {"on": True}})
    receipt = executor.execute(operation, make_fixture())

    assert receipt == {"ok": True, "provider": "hue_v2", "op": "hue.put_light", "detail": "HTTP 200"}
    assert len(fake.put_calls) == 1
    call = fake.put_calls[0]
    assert call["url"] == "https://192.0.2.10/clip/v2/resource/light/rid-123"
    assert call["headers"]["hue-application-key"] == "example-hue-appkey"  # secrets-scan:allow (placeholder)
    assert call["json"] == {"on": {"on": True}}
    assert call["verify"] is False  # legacy parity; hardening TODO R1
    assert call["timeout"] == 5.0


def test_hue_put_with_200_status_but_bridge_errors_is_not_ok():
    """Live-verified against a real bridge: CLIP v2 returns HTTP 200 with a
    structured {"errors": [...], "data": [...]} body even for a clean
    write. A non-empty errors array is the bridge telling us it rejected
    part of the payload — the status code alone would hide that."""
    fake = FakeRequests(put_json_body={"errors": [{"description": "brightness: not settable"}], "data": []})
    executor = RequestsProviderExecutor(
        hue_ip="192.0.2.10",
        hue_username="example-hue-appkey",  # secrets-scan:allow (placeholder)
        requests_module=fake,
    )
    receipt = executor.execute(make_operation("hue_v2", "hue.put_light", "rid-123", {"dimming": {"brightness": 60}}), make_fixture())
    assert receipt["ok"] is False
    assert "brightness: not settable" in receipt["detail"]
    assert "HTTP 200" in receipt["detail"]


def test_hue_put_with_200_status_and_empty_errors_is_ok():
    fake = FakeRequests(put_json_body={"errors": [], "data": [{"rid": "rid-123", "rtype": "light"}]})
    executor = RequestsProviderExecutor(
        hue_ip="192.0.2.10",
        hue_username="example-hue-appkey",  # secrets-scan:allow (placeholder)
        requests_module=fake,
    )
    receipt = executor.execute(make_operation("hue_v2", "hue.put_light", "rid-123", {"on": {"on": True}}), make_fixture())
    assert receipt == {"ok": True, "provider": "hue_v2", "op": "hue.put_light", "detail": "HTTP 200"}


def test_hue_put_with_non_json_200_body_still_ok_on_status_alone():
    # FakeRequests() defaults to no json_body -> FakeResponse.json() raises,
    # mirroring a bridge/proxy returning a non-JSON 200. Status-only fallback.
    fake = FakeRequests()
    executor = RequestsProviderExecutor(
        hue_ip="192.0.2.10",
        hue_username="example-hue-appkey",  # secrets-scan:allow (placeholder)
        requests_module=fake,
    )
    receipt = executor.execute(make_operation("hue_v2", "hue.put_light", "rid-123", {"on": {"on": True}}), make_fixture())
    assert receipt["ok"] is True


def test_confirmation_never_fires_for_a_native_hue_fixture():
    """No extra GET, no sleep, single PUT — the confirm hook only applies to
    fixtures whose capabilities explicitly report dynamic_native: false."""
    fake = FakeRequests()
    sleeps = []
    executor = RequestsProviderExecutor(
        hue_ip="192.0.2.10", hue_username="key", requests_module=fake, sleep_fn=sleeps.append  # secrets-scan:allow (placeholder)
    )
    fixture = make_hue_fixture(dynamic_native=True)
    receipt = executor.execute(make_operation("hue_v2", "hue.put_light", "rid-123", {"on": {"on": True}}), fixture)
    assert receipt["ok"] is True
    assert len(fake.put_calls) == 1
    assert fake.get_calls == []
    assert sleeps == []


def test_confirmation_never_fires_when_fixture_capabilities_are_unknown():
    """A fixture with no assessed capabilities is NOT treated as non-native
    by default — confirmation is opt-in by evidence."""
    fake = FakeRequests()
    executor = RequestsProviderExecutor(hue_ip="192.0.2.10", hue_username="key", requests_module=fake)  # secrets-scan:allow (placeholder)
    receipt = executor.execute(make_operation("hue_v2", "hue.put_light", "rid-123", {"on": {"on": True}}), make_fixture())
    assert receipt["ok"] is True
    assert fake.get_calls == []


def test_confirmation_succeeds_when_readback_matches_the_payload():
    fake = FakeRequests(
        put_json_body={"errors": [], "data": [{"rid": "rid-123", "rtype": "light"}]},
        get_json_bodies=[{"data": [{"on": {"on": True}, "dimming": {"brightness": 60.2}}]}],
    )
    sleeps = []
    executor = RequestsProviderExecutor(
        hue_ip="192.0.2.10", hue_username="key", requests_module=fake, sleep_fn=sleeps.append  # secrets-scan:allow (placeholder)
    )
    fixture = make_hue_fixture()
    op = make_operation("hue_v2", "hue.put_light", "rid-123", {"on": {"on": True}, "dimming": {"brightness": 60.0}})
    receipt = executor.execute(op, fixture)
    assert receipt["ok"] is True
    assert "confirmed by read-back" in receipt["detail"]
    assert len(fake.put_calls) == 1  # confirmed on the first attempt, no retry needed
    assert len(fake.get_calls) == 1
    assert sleeps == [0.35]  # only the read-back delay, no retry backoff


def test_communication_error_gets_the_escalating_retry_budget():
    """Live-verified against a real bridge: a flaky third-party light fails
    every field with error_code "communication_error" on a 207. That
    specific failure gets 3 retries with growing backoff (mesh route may
    self-heal), not the conservative single retry other failures get."""
    fake = FakeRequests(
        put_status=207,
        put_json_body={
            "data": [{"rid": "rid-123", "rtype": "light"}],
            "errors": [
                {"description": "device has communication issues, command (.on.on) may not have effect", "error_code": "communication_error"},
            ],
        },
    )
    sleeps = []
    executor = RequestsProviderExecutor(
        hue_ip="192.0.2.10", hue_username="key", requests_module=fake, sleep_fn=sleeps.append  # secrets-scan:allow (placeholder)
    )
    fixture = make_hue_fixture()
    receipt = executor.execute(make_operation("hue_v2", "hue.put_light", "rid-123", {"on": {"on": True}}), fixture)
    assert receipt["ok"] is False
    assert "communication issues" in receipt["detail"]
    assert len(fake.put_calls) == 4  # 1 initial + 3 retries
    assert sleeps == [0.4, 0.8, 1.5]  # escalating backoff, no readback (the PUT itself never succeeded)


def test_communication_error_recovers_partway_through_the_escalated_retries():
    fake = FakeRequests(
        put_statuses=[207, 207, 200],
        put_json_bodies=[
            {"data": [], "errors": [{"description": "comm issue", "error_code": "communication_error"}]},
            {"data": [], "errors": [{"description": "comm issue", "error_code": "communication_error"}]},
            {"data": [{"rid": "rid-123", "rtype": "light"}], "errors": []},
        ],
        get_json_bodies=[{"data": [{"on": {"on": True}}]}],
    )
    executor = RequestsProviderExecutor(
        hue_ip="192.0.2.10", hue_username="key", requests_module=fake, sleep_fn=lambda s: None  # secrets-scan:allow (placeholder)
    )
    fixture = make_hue_fixture()
    receipt = executor.execute(make_operation("hue_v2", "hue.put_light", "rid-123", {"on": {"on": True}}), fixture)
    assert receipt["ok"] is True
    assert "confirmed by read-back" in receipt["detail"]
    assert len(fake.put_calls) == 3


def test_a_non_communication_bridge_error_gets_only_the_conservative_retry():
    """A validation-shaped rejection (not "communication_error") won't be
    fixed by waiting for the mesh — one retry, not the escalated budget."""
    fake = FakeRequests(
        put_json_body={"data": [], "errors": [{"description": "brightness: not settable", "error_code": "invalid_value"}]},
    )
    sleeps = []
    executor = RequestsProviderExecutor(
        hue_ip="192.0.2.10", hue_username="key", requests_module=fake, sleep_fn=sleeps.append  # secrets-scan:allow (placeholder)
    )
    fixture = make_hue_fixture()
    receipt = executor.execute(make_operation("hue_v2", "hue.put_light", "rid-123", {"dimming": {"brightness": 200}}), fixture)
    assert receipt["ok"] is False
    assert len(fake.put_calls) == 2  # 1 initial + 1 conservative retry
    assert sleeps == [0.3]


def test_confirmation_retries_then_fails_when_light_never_confirms():
    """A dropped Zigbee command: the bridge accepts the PUT every time
    (errors: []) but the light itself never reflects the change — retried
    once, then reported as a real failure instead of a false 'ok'."""
    fake = FakeRequests(
        put_json_body={"errors": [], "data": [{"rid": "rid-123", "rtype": "light"}]},
        get_json_bodies=[{"data": [{"on": {"on": False}}]}],  # never turns on, every read-back
    )
    sleeps = []
    executor = RequestsProviderExecutor(
        hue_ip="192.0.2.10", hue_username="key", requests_module=fake, sleep_fn=sleeps.append  # secrets-scan:allow (placeholder)
    )
    fixture = make_hue_fixture()
    op = make_operation("hue_v2", "hue.put_light", "rid-123", {"on": {"on": True}})
    receipt = executor.execute(op, fixture)
    assert receipt["ok"] is False
    assert "not confirmed by read-back" in receipt["detail"]
    assert "on: wanted True, observed False" in receipt["detail"]
    assert len(fake.put_calls) == 2  # one retry attempted
    assert len(fake.get_calls) == 2
    assert sleeps == [0.35, 0.3, 0.35]  # readback, retry backoff, readback again


def test_confirmation_succeeds_on_the_retry_after_an_initial_miss():
    fake = FakeRequests(
        put_json_body={"errors": [], "data": [{"rid": "rid-123", "rtype": "light"}]},
        get_json_bodies=[{"data": [{"on": {"on": False}}]}, {"data": [{"on": {"on": True}}]}],
    )
    executor = RequestsProviderExecutor(
        hue_ip="192.0.2.10", hue_username="key", requests_module=fake, sleep_fn=lambda s: None  # secrets-scan:allow (placeholder)
    )
    fixture = make_hue_fixture()
    receipt = executor.execute(make_operation("hue_v2", "hue.put_light", "rid-123", {"on": {"on": True}}), fixture)
    assert receipt["ok"] is True
    assert "confirmed by read-back" in receipt["detail"]
    assert len(fake.put_calls) == 2
    assert len(fake.get_calls) == 2


def test_confirmation_treats_a_failed_readback_as_unconfirmed_not_a_crash():
    fake = FakeRequests(
        put_json_body={"errors": [], "data": [{"rid": "rid-123", "rtype": "light"}]},
        get_status=500,  # read-back itself fails
    )
    executor = RequestsProviderExecutor(
        hue_ip="192.0.2.10", hue_username="key", requests_module=fake, sleep_fn=lambda s: None  # secrets-scan:allow (placeholder)
    )
    fixture = make_hue_fixture()
    receipt = executor.execute(make_operation("hue_v2", "hue.put_light", "rid-123", {"on": {"on": True}}), fixture)
    assert receipt["ok"] is False
    assert "read-back for confirmation failed" in receipt["detail"]


def test_confirmation_within_numeric_tolerance_still_counts_as_confirmed():
    """Bridge-side rounding/gamut remapping is not a real mismatch."""
    fake = FakeRequests(
        put_json_body={"errors": [], "data": [{"rid": "rid-123", "rtype": "light"}]},
        get_json_bodies=[{"data": [{"dimming": {"brightness": 59.4}}]}],  # requested 60.0
    )
    executor = RequestsProviderExecutor(
        hue_ip="192.0.2.10", hue_username="key", requests_module=fake, sleep_fn=lambda s: None  # secrets-scan:allow (placeholder)
    )
    fixture = make_hue_fixture()
    receipt = executor.execute(
        make_operation("hue_v2", "hue.put_light", "rid-123", {"dimming": {"brightness": 60.0}}), fixture
    )
    assert receipt["ok"] is True


def test_hue_identify_puts_clip_v2_identify_action():
    fake = FakeRequests()
    executor = RequestsProviderExecutor(
        hue_ip="192.0.2.10",
        hue_username="example-hue-appkey",  # secrets-scan:allow (placeholder)
        requests_module=fake,
    )
    operation = make_operation("hue_v2", "hue.identify", "rid-123", {"identify": {"action": "identify"}})
    receipt = executor.execute(operation, make_fixture())

    assert receipt == {"ok": True, "provider": "hue_v2", "op": "hue.identify", "detail": "HTTP 200"}
    call = fake.put_calls[0]
    assert call["url"] == "https://192.0.2.10/clip/v2/resource/light/rid-123"
    assert call["json"] == {"identify": {"action": "identify"}}


def test_hue_put_scene_dynamic_is_never_sent_to_a_generic_light_endpoint():
    fake = FakeRequests()
    executor = RequestsProviderExecutor(
        hue_ip="192.0.2.10",
        hue_username="example-hue-appkey",  # secrets-scan:allow (placeholder)
        requests_module=fake,
    )
    operation = make_operation("hue_v2", "hue.put_scene_dynamic", "rid-123", {"speed": 0.4, "palette": ["#112233"]})
    receipt = executor.execute(operation, make_fixture())
    assert receipt["ok"] is False
    assert "managed-scene realization" in receipt["detail"]
    assert fake.put_calls == []


def test_hue_missing_credentials_returns_failed_receipt_without_calling_http():
    fake = FakeRequests()
    executor = RequestsProviderExecutor(hue_ip=None, hue_username=None, requests_module=fake)
    receipt = executor.execute(make_operation("hue_v2", "hue.put_light", "rid-1"), make_fixture())
    assert receipt["ok"] is False
    assert "hue_ip/hue_username not configured" in receipt["detail"]
    assert fake.put_calls == []


def test_wled_posts_state_payload():
    fake = FakeRequests()
    executor = RequestsProviderExecutor(wled_endpoint="wled-938664.local", requests_module=fake)
    payload = {"on": True, "bri": 128, "seg": [{"id": 0, "col": [[255, 0, 0]]}]}
    receipt = executor.execute(make_operation("wled", "wled.post_state", "aabbccddeeff:seg:0", payload), make_fixture())

    assert receipt["ok"] is True
    assert receipt["provider"] == "wled"
    call = fake.post_calls[0]
    assert call["url"] == "http://wled-938664.local/json/state"
    assert call["json"] == payload

    executor_bad = RequestsProviderExecutor(wled_endpoint="wled-938664.local", requests_module=FakeRequests(post_status=500))
    receipt = executor_bad.execute(make_operation("wled", "wled.post_state", "x"), make_fixture())
    assert receipt["ok"] is False
    assert "HTTP 500" in receipt["detail"]


def test_wled_missing_endpoint_returns_failed_receipt():
    executor = RequestsProviderExecutor(wled_endpoint=None, requests_module=FakeRequests())
    receipt = executor.execute(make_operation("wled", "wled.post_state", "x"), make_fixture())
    assert receipt["ok"] is False
    assert "no WLED endpoint resolvable" in receipt["detail"]


def test_ha_light_ops_bridge_to_call_service():
    calls = []

    def call_service(service, **payload):
        calls.append((service, payload))

    executor = RequestsProviderExecutor(call_service=call_service, requests_module=FakeRequests())
    on = executor.execute(make_operation("ha_light", "ha.call_light", "light.lamp", {"entity_id": "light.lamp"}), make_fixture())
    off = executor.execute(make_operation("ha_light", "ha.call_light_off", "light.lamp", {"entity_id": "light.lamp"}), make_fixture())

    assert on["ok"] is True and off["ok"] is True
    assert calls == [
        ("light/turn_on", {"entity_id": "light.lamp"}),
        ("light/turn_off", {"entity_id": "light.lamp"}),
    ]


def test_ha_light_identify_bridges_to_turn_on_with_flash():
    calls = []

    def call_service(service, **payload):
        calls.append((service, payload))

    executor = RequestsProviderExecutor(call_service=call_service, requests_module=FakeRequests())
    receipt = executor.execute(
        make_operation("ha_light", "ha.call_light_identify", "light.lamp", {"entity_id": "light.lamp", "flash": "short"}),
        make_fixture(),
    )
    assert receipt["ok"] is True
    assert calls == [("light/turn_on", {"entity_id": "light.lamp", "flash": "short"})]


def test_ha_light_without_bridge_returns_failed_receipt():
    executor = RequestsProviderExecutor(requests_module=FakeRequests())
    receipt = executor.execute(make_operation("ha_light", "ha.call_light", "light.lamp"), make_fixture())
    assert receipt["ok"] is False


def test_unknown_provider_and_transport_exceptions_become_failed_receipts():
    executor = RequestsProviderExecutor(requests_module=FakeRequests())
    receipt = executor.execute(make_operation("fallback", "noop", "x"), make_fixture())
    assert receipt["ok"] is False
    assert "unknown provider" in receipt["detail"]

    breaker = RequestsProviderExecutor(
        hue_ip="192.0.2.10",
        hue_username="example-hue-appkey",  # secrets-scan:allow (placeholder)
        requests_module=FakeRequests(raise_exc=ConnectionError("bridge down")),
    )
    receipt = breaker.execute(make_operation("hue_v2", "hue.put_light", "rid-1"), make_fixture())
    assert receipt["ok"] is False
    assert "ConnectionError" in receipt["detail"]
    assert "bridge down" in receipt["detail"]


# ---------------------------------------------------------------------------
# multi-WLED endpoint resolution (per-binding routing; host is not identity)
# ---------------------------------------------------------------------------

def make_wled_fixture(fixture_id, device_id, endpoint_hint=None, segment_ids=(0,)):
    binding = {"provider": "wled", "device_id": device_id, "segment_ids": list(segment_ids)}
    if endpoint_hint:
        binding["endpoint_hint"] = endpoint_hint
    return Fixture.from_dict({"id": fixture_id, "name": fixture_id, "binding": binding})


WLED_OP = dict(provider="wled", op="wled.post_state", payload={"seg": [{"id": 0, "on": True}]})


def test_two_devices_route_to_their_own_endpoint_hints():
    requests = FakeRequests()
    executor = RequestsProviderExecutor(wled_endpoint="wled.local", requests_module=requests)
    studio = make_wled_fixture("wled_seg_0", "aabbccddeeff", endpoint_hint="http://wled.local")
    den = make_wled_fixture("den_seg_0", "112233445566", endpoint_hint="192.0.2.91")

    executor.execute(make_operation(resource_ref="aabbccddeeff:seg:0", **WLED_OP), studio)
    executor.execute(make_operation(resource_ref="112233445566:seg:0", **WLED_OP), den)

    assert [call["url"] for call in requests.post_calls] == [
        "http://wled.local/json/state",
        "http://192.0.2.91/json/state",
    ]


def test_segments_of_one_device_share_one_controller():
    requests = FakeRequests()
    executor = RequestsProviderExecutor(wled_endpoint="wled.local", requests_module=requests)
    bank = make_wled_fixture("wled_bank", "aabbccddeeff", endpoint_hint="wled.local", segment_ids=(0, 1, 2))
    op = make_operation(resource_ref="aabbccddeeff:seg:0,1,2", payload={"seg": [{"id": i} for i in (0, 1, 2)]},
                        provider="wled", op="wled.post_state")

    executor.execute(op, bank)

    assert len(requests.post_calls) == 1
    assert requests.post_calls[0]["url"] == "http://wled.local/json/state"


def test_global_wled_host_is_the_fallback_when_binding_has_no_hint():
    requests = FakeRequests()
    executor = RequestsProviderExecutor(wled_endpoint="wled.local", requests_module=requests)
    legacy_fixture = make_wled_fixture("wled_seg_3", "aabbccddeeff", endpoint_hint=None)

    receipt = executor.execute(make_operation(resource_ref="aabbccddeeff:seg:3", **WLED_OP), legacy_fixture)

    assert receipt["ok"] is True
    assert requests.post_calls[0]["url"] == "http://wled.local/json/state"


def test_discovery_device_map_beats_hint_beats_global():
    requests = FakeRequests()
    executor = RequestsProviderExecutor(
        wled_endpoint="wled.local",
        wled_endpoints={"112233445566": "192.0.2.91"},  # discovery-populated seam
        requests_module=requests,
    )
    moved = make_wled_fixture("den_seg_0", "112233445566", endpoint_hint="192.0.2.200")  # stale bind-time hint

    executor.execute(make_operation(resource_ref="112233445566:seg:0", **WLED_OP), moved)

    assert requests.post_calls[0]["url"] == "http://192.0.2.91/json/state"


def test_endpoint_hint_url_form_is_normalized():
    requests = FakeRequests()
    executor = RequestsProviderExecutor(wled_endpoint=None, requests_module=requests)
    fixture = make_wled_fixture("wled_seg_0", "aabbccddeeff", endpoint_hint="http://wled.local/")

    receipt = executor.execute(make_operation(resource_ref="aabbccddeeff:seg:0", **WLED_OP), fixture)

    assert receipt["ok"] is True
    assert requests.post_calls[0]["url"] == "http://wled.local/json/state"


def test_unresolvable_endpoint_fails_per_fixture_without_raising():
    executor = RequestsProviderExecutor(wled_endpoint=None, requests_module=FakeRequests())
    orphan = make_wled_fixture("wled_orphan", "ffffffffffff", endpoint_hint=None)

    receipt = executor.execute(make_operation(resource_ref="ffffffffffff:seg:0", **WLED_OP), orphan)

    assert receipt["ok"] is False
    assert "wled_orphan" in receipt["detail"]
