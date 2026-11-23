"""scripts/provision_node.py: the generated secrets.h gives the firmware the same bytes; bad input is refused."""

from __future__ import annotations

import importlib.util
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("provision_node", ROOT / "scripts" / "provision_node.py")
assert SPEC is not None and SPEC.loader is not None
pn = importlib.util.module_from_spec(SPEC)
sys.modules["provision_node"] = pn
SPEC.loader.exec_module(pn)

CA = "-----BEGIN CERTIFICATE-----\nQUJD+/ZGRw==\nREVG\n-----END CERTIFICATE-----\n"
PRINT_MACROS = """#include <stdio.h>
#include "secrets.h"
int main(void) {
  fputs(KN_WIFI_SSID, stdout); fputc(0, stdout); fputs(KN_WIFI_PASS, stdout); fputc(0, stdout);
  fputs(KN_MQTT_HOST, stdout); fputc(0, stdout); printf("%d", KN_MQTT_PORT); fputc(0, stdout);
  fputs(KN_MQTT_USER, stdout); fputc(0, stdout); fputs(KN_MQTT_PASS, stdout); fputc(0, stdout);
  fputs(KN_DEVICE_ID, stdout); fputc(0, stdout); fputs(KN_MQTT_CA_PEM, stdout);
  return 0;
}
"""


def compiled_values(tmp_path: Path, header: str) -> list[str]:
    """What a C compiler really reads from the header: the same language the firmware is built in."""
    cc = shutil.which("cc")
    if cc is None:
        pytest.skip("no C compiler")
    (tmp_path / "secrets.h").write_text(header, encoding="ascii")
    (tmp_path / "p.c").write_text(PRINT_MACROS, encoding="ascii")
    subprocess.run([cc, "-Wall", "-Werror", "-trigraphs", "p.c", "-o", "p"], cwd=tmp_path, check=True)
    return subprocess.run([tmp_path / "p"], capture_output=True, check=True).stdout.decode().split("\0")


@pytest.mark.parametrize(
    ("ssid", "wifi_pass"),
    [
        ("MyHotspot", "plain-password"),
        ('Ari"s \\ phone', 'pa"ss\\word %s %d'),
        ("ਮੇਰਾ ਫ਼ੋਨ", "ends-in-backslash\\"),
        ("a?b", "tri??graph??=x"),
        ("é1", "digit-after-escape-1"),
    ],
)
def test_the_compiler_reads_back_the_same_values(tmp_path: Path, ssid: str, wifi_pass: str) -> None:
    header = pn.render_secrets_h(
        ssid=ssid,
        wifi_pass=wifi_pass,
        host="192.168.43.10",
        port=8883,
        device_id="kn-node-001",
        mqtt_pass="ab12",
        ca_pem=CA,
    )
    assert compiled_values(tmp_path, header) == [
        ssid,
        wifi_pass,
        "192.168.43.10",
        "8883",
        "kn-node-001",
        "ab12",
        "kn-node-001",
        CA,
    ]


def test_header_defines_exactly_what_the_template_defines() -> None:
    header = pn.render_secrets_h(
        ssid="s", wifi_pass="password", host="h.example", port=8883, device_id="kn-node-001", mqtt_pass="p", ca_pem=CA
    )
    template = (ROOT / "firmware" / "include" / "secrets.example.h").read_text()

    def names(text: str) -> set[str]:
        return {ln.split()[1] for ln in text.splitlines() if ln.startswith("#define")}

    assert names(header) == names(template)


@pytest.mark.parametrize(
    "pem", ["", "not a certificate", "-----BEGIN PRIVATE KEY-----\nQUJD\n-----END PRIVATE KEY-----\n"]
)
def test_a_ca_file_that_is_not_a_certificate_is_refused(pem: str) -> None:
    with pytest.raises(pn.ProvisionError):
        pn.render_secrets_h(
            ssid="s", wifi_pass="password", host="h", port=8883, device_id="kn-x", mqtt_pass="p", ca_pem=pem
        )


@pytest.mark.parametrize(
    "device_id", ["", "ab", "KN-NODE", "kn_node", "-kn-node", "a" * 33, "kn/node", "kn-node-001\n", "ingestion"]
)
def test_bad_device_ids_are_refused(device_id: str) -> None:
    with pytest.raises(pn.ProvisionError):
        pn.check_device_id(device_id)


@pytest.mark.parametrize("device_id", ["kn-node-001", "abc", "a" * 32])
def test_good_device_ids_pass(device_id: str) -> None:
    assert pn.check_device_id(device_id) == device_id


@pytest.mark.parametrize("host", ["", "a b", "1.2.3.4;rm", "$(x)", "-x", "host\n"])
def test_bad_hosts_are_refused(host: str) -> None:
    with pytest.raises(pn.ProvisionError):
        pn.check_host(host)


@pytest.mark.parametrize("ssid", ["", "x" * 33, "ਫ" * 11, "a\nb"])
def test_bad_ssids_are_refused(ssid: str) -> None:
    with pytest.raises(pn.ProvisionError):
        pn.check_ssid(ssid)


@pytest.mark.parametrize("password", ["", "short12", "x" * 64, "x" * 65, "new\nline-pass", "ਪਾਸਵਰਡ-ਪਾਸਵਰਡ"])
def test_bad_wifi_passwords_are_refused_without_repeating_them(password: str) -> None:
    with pytest.raises(pn.ProvisionError) as e:
        pn.check_wifi_pass(password)
    if password:
        assert password not in str(e.value)


@pytest.mark.parametrize("password", ["12345678", "x" * 63, "AB12" * 16])
def test_good_wifi_passwords_pass(password: str) -> None:
    assert pn.check_wifi_pass(password) == password


def test_a_tracked_path_in_the_public_repository_is_refused() -> None:
    with pytest.raises(pn.ProvisionError, match="not git-ignored"):
        pn.check_out_path(ROOT / "firmware" / "include" / "secrets_copy.h")


def test_the_default_path_is_git_ignored() -> None:
    assert pn.check_out_path(pn.DEFAULT_OUT) == pn.DEFAULT_OUT.resolve()


def test_written_file_is_owner_only_and_leaves_no_temporary(tmp_path: Path) -> None:
    out = tmp_path / "secrets.h"
    pn.write_private(out, "x\n")
    pn.write_private(out, "y\n")
    assert out.read_text() == "y\n"
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert [p.name for p in tmp_path.iterdir()] == ["secrets.h"]


def test_a_failed_step_does_not_show_its_command_line() -> None:
    code = "import sys; sys.exit(3)"
    with pytest.raises(pn.ProvisionError) as e:
        pn.run_step("the step", [sys.executable, "-c", code, "the-secret-argument"])
    assert "the-secret-argument" not in str(e.value)
