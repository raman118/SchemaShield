import json
import threading
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import psycopg
import pytest
from docker.errors import DockerException
from pydantic import ValidationError
from schemashield.config import Settings
from schemashield.sandbox import OWNER_LABEL, SandboxRunner, SandboxUnavailable


def fake_client(base_url, *, verify=True, cert=("client-cert", "client-key")):
    return SimpleNamespace(
        api=SimpleNamespace(base_url=base_url, verify=verify, cert=cert),
        close=Mock(),
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://10.10.1.1:2375",
        "http://127.0.0.1:2375",
        "https://remote:2376",
        "https://127.0.0.1:2376.attacker",
        "http+docker://ssh",
        "http+docker://remote",
    ],
)
def test_remote_and_insecure_docker_transports_are_rejected(monkeypatch, url):
    client = fake_client(url)
    monkeypatch.setattr("schemashield.sandbox.docker.from_env", lambda **kwargs: client)
    with pytest.raises(SandboxUnavailable, match="local"):
        SandboxRunner(Settings()).client()
    client.close.assert_called_once()


@pytest.mark.parametrize("url", ["http+docker://localhost", "http+docker://localnpipe"])
def test_local_socket_transports_are_accepted(monkeypatch, url):
    client = fake_client(url, verify=False, cert=None)
    monkeypatch.setattr("schemashield.sandbox.docker.from_env", lambda **kwargs: client)
    assert SandboxRunner(Settings()).client() is client
    client.close.assert_not_called()


@pytest.mark.parametrize("verify,cert", [(False, ("cert", "key")), (True, None)])
def test_loopback_gateway_requires_certificate_verification_and_client_identity(
    monkeypatch, verify, cert
):
    client = fake_client("https://127.0.0.1:2376", verify=verify, cert=cert)
    monkeypatch.setattr("schemashield.sandbox.docker.from_env", lambda **kwargs: client)
    with pytest.raises(SandboxUnavailable):
        SandboxRunner(Settings()).client()
    client.close.assert_called_once()


def test_verified_mutual_tls_loopback_gateway_is_accepted(monkeypatch):
    client = fake_client("https://127.0.0.1:2376", verify="trusted-ca.pem")
    monkeypatch.setattr("schemashield.sandbox.docker.from_env", lambda **kwargs: client)
    assert SandboxRunner(Settings()).client() is client


def test_docker_initialization_errors_are_sanitized(monkeypatch):
    def failed(**kwargs):
        raise DockerException("private-password-and-host-path")

    monkeypatch.setattr("schemashield.sandbox.docker.from_env", failed)
    with pytest.raises(SandboxUnavailable) as caught:
        SandboxRunner(Settings()).client()
    assert "private-password" not in str(caught.value)


@pytest.mark.parametrize("host", ["tcp://0.0.0.0:2376", "tcp://127.0.0.1:2375", "ssh://remote"])
def test_administrator_docker_host_setting_is_still_local_only(host):
    with pytest.raises(ValidationError, match="mutual TLS"):
        Settings(docker_host=host)


def resource(owner, expires=None):
    labels = {OWNER_LABEL: owner}
    if expires is not None:
        labels["io.schemashield.expires"] = str(expires)
    return SimpleNamespace(labels=labels, attrs={"Labels": labels}, remove=Mock())


def test_reaper_removes_only_own_expired_leases_even_with_unexpected_list_results(monkeypatch):
    runner = SandboxRunner(Settings(sandbox_enabled=True, api_token="unit-test-execution-token-32"))
    expired = resource(runner.instance_id, 99)
    active = resource(runner.instance_id, 101)
    unleased = resource(runner.instance_id)
    foreign = resource("another-installation", 99)
    network = resource(runner.instance_id, 99)
    foreign_network = resource("another-installation", 99)
    client = SimpleNamespace(
        containers=SimpleNamespace(list=Mock(return_value=[expired, active, unleased, foreign])),
        networks=SimpleNamespace(list=Mock(return_value=[network, foreign_network])),
        close=Mock(),
    )
    monkeypatch.setattr(runner, "client", lambda **kwargs: client)
    monkeypatch.setattr("schemashield.sandbox.time.time", lambda: 100)
    runner.cleanup_expired()
    expired.remove.assert_called_once_with(force=True, v=True)
    network.remove.assert_called_once_with()
    for preserved in (active, unleased, foreign, foreign_network):
        preserved.remove.assert_not_called()
    client.close.assert_called_once()
    client.containers.list.assert_called_once_with(
        all=True, filters={"label": f"{OWNER_LABEL}={runner.instance_id}"}
    )


def test_reaper_malformed_lease_cannot_prevent_other_owned_cleanup(monkeypatch):
    runner = SandboxRunner(Settings(sandbox_enabled=True, api_token="unit-test-execution-token-32"))
    malformed = resource(runner.instance_id, "invalid")
    expired = resource(runner.instance_id, 99)
    client = SimpleNamespace(
        containers=SimpleNamespace(list=Mock(return_value=[malformed, expired])),
        networks=SimpleNamespace(list=Mock(return_value=[])),
        close=Mock(),
    )
    monkeypatch.setattr(runner, "client", lambda **kwargs: client)
    monkeypatch.setattr("schemashield.sandbox.time.time", lambda: 100)
    runner.cleanup_expired()
    malformed.remove.assert_not_called()
    expired.remove.assert_called_once_with(force=True, v=True)
    client.close.assert_called_once()


def test_disabled_reaper_never_opens_docker(monkeypatch):
    runner = SandboxRunner(Settings(sandbox_enabled=False))
    client = Mock()
    monkeypatch.setattr(runner, "client", client)
    runner.cleanup_expired()
    client.assert_not_called()


def test_owned_reaper_identity_is_stable_across_runner_restart():
    settings = Settings(database_url="postgresql+psycopg://operator:private@localhost/control")
    first = SandboxRunner(settings)
    assert first.instance_id == SandboxRunner(settings).instance_id
    assert "private" not in first.instance_id
    assert (
        first.instance_id
        != SandboxRunner(
            Settings(database_url="postgresql+psycopg://other@localhost/other")
        ).instance_id
    )


def test_failed_instrumentation_cannot_report_success_or_expose_credentials(monkeypatch):
    runner = SandboxRunner(Settings(sandbox_enabled=True, api_token="unit-test-execution-token-32"))
    real_event = threading.Event

    class QuickEvent(real_event):
        def wait(self, timeout=None):
            return super().wait(0.05 if timeout == 3 else timeout)

    @contextmanager
    def environment(cancelled):
        admin = SimpleNamespace(
            execute=lambda statement: SimpleNamespace(fetchone=lambda: (2000, "hash"))
        )
        yield ({"password": "private-fixture-password"}, admin, {"fixture_rows": 2000})

    def failed_connection(**kwargs):
        raise psycopg.OperationalError("private-fixture-password must never appear in report")

    monkeypatch.setattr(runner, "environment", environment)
    monkeypatch.setattr("schemashield.sandbox.psycopg.connect", failed_connection)
    monkeypatch.setattr("schemashield.sandbox.threading.Event", QuickEvent)
    report = runner.run(["ALTER TABLE accounts ADD COLUMN boundary_check text"])
    assert report["status"] == "failed"
    assert report["checks"] == []
    assert report["metrics"]["instrumentation_errors"] == ["OperationalError", "OperationalError"]
    assert "private-fixture-password" not in json.dumps(report)


def test_partial_environment_creation_cleans_owned_network_and_sanitizes_error(monkeypatch):
    runner = SandboxRunner(Settings(sandbox_enabled=True, api_token="unit-test-execution-token-32"))
    network = SimpleNamespace(name="owned-test-network", remove=Mock())
    client = SimpleNamespace(
        images=SimpleNamespace(get=Mock()),
        networks=SimpleNamespace(create=Mock(return_value=network)),
        containers=SimpleNamespace(run=Mock(side_effect=DockerException("private-host-path"))),
        close=Mock(),
    )
    monkeypatch.setattr(runner, "client", lambda **kwargs: client)
    with pytest.raises(SandboxUnavailable) as caught:
        with runner.environment():
            pytest.fail("An environment cannot be yielded after failed container creation")
    assert "private-host-path" not in str(caught.value)
    network.remove.assert_called_once_with()
    client.close.assert_called_once()
    kwargs = client.networks.create.call_args.kwargs
    assert kwargs["internal"] is True
    assert kwargs["labels"][OWNER_LABEL] == runner.instance_id
    assert "io.schemashield.expires" in kwargs["labels"]


def test_cleanup_failure_is_explicit_instead_of_silent_success(monkeypatch):
    runner = SandboxRunner(Settings(sandbox_enabled=True, api_token="unit-test-execution-token-32"))
    network = SimpleNamespace(
        name="owned-test-network",
        remove=Mock(side_effect=DockerException("private-service-detail")),
    )
    client = SimpleNamespace(
        images=SimpleNamespace(get=Mock()),
        networks=SimpleNamespace(create=Mock(return_value=network)),
        containers=SimpleNamespace(run=Mock(side_effect=DockerException("private-host-detail"))),
        close=Mock(),
    )
    monkeypatch.setattr(runner, "client", lambda **kwargs: client)
    with pytest.raises(SandboxUnavailable, match="cleanup failed") as caught:
        with runner.environment():
            pytest.fail("A failed environment cannot be yielded")
    assert "private-" not in str(caught.value)
    client.close.assert_called_once()
