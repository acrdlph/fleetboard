"""Doc-consistency guard: the transport docs must not re-assert TLS/SPKI pinning
as the shipping contract once ADR 0013 (plain HTTP over the tailnet) is Accepted.

Docs-only reconciliation — see docs/mobile/adr/0013-plain-http-over-the-tailnet.md.
This test reads the Markdown and asserts the specific present-tense lies the review
flagged are gone and the ADR 0013 annotations are present. It touches no package
code and binds no ports.
"""
import unittest
from pathlib import Path

DOCS = Path(__file__).resolve().parent.parent / "docs" / "mobile"
ADR0013 = DOCS / "adr" / "0013-plain-http-over-the-tailnet.md"
ARCH = DOCS / "ARCHITECTURE.md"
API = DOCS / "API.md"


class TestAdr0013IsAuthoritative(unittest.TestCase):
    def test_adr_exists_and_accepted(self):
        self.assertTrue(ADR0013.exists(), "ADR 0013 must exist")
        text = ADR0013.read_text()
        self.assertIn("Status:", text)
        self.assertIn("Accepted", text)


class TestArchitectureTransportReconciled(unittest.TestCase):
    def setUp(self):
        self.text = ARCH.read_text()

    def test_section_5_4_has_superseded_banner(self):
        self.assertIn(
            "SUPERSEDED by [ADR 0013](adr/0013-plain-http-over-the-tailnet.md)",
            self.text,
        )

    def test_old_listener_table_line_is_gone(self):
        # The un-annotated present-tense claim the review flagged.
        self.assertNotIn(
            "tailnet  100.x.y.z:4242   TLS, self-signed P-256, SPKI-pinned",
            self.text,
        )

    def test_listener_table_now_says_plain_http(self):
        self.assertIn("plain HTTP (ADR 0013) · bearer token · NO HTML", self.text)

    def test_threat_model_names_the_magicdns_residual(self):
        # §5.1 must state the residual honestly rather than claim pinning closes it.
        self.assertIn("Reconciled with [ADR 0013]", self.text)
        self.assertIn("spoofed MagicDNS record", self.text)
        self.assertIn("tailnet ACL", self.text)

    def test_open_question_d1_marked_resolved(self):
        self.assertIn("Resolved by [ADR 0013]", self.text)

    def test_no_unannotated_present_tense_tls_heading_claim(self):
        # The historical "Why TLS" argument is retained but must be framed as historical.
        self.assertIn("Why TLS was considered at all", self.text)


class TestApiTransportReconciled(unittest.TestCase):
    def setUp(self):
        self.text = API.read_text()

    def test_base_url_section_no_longer_asserts_https(self):
        self.assertNotIn(
            "The tailnet listeners are **HTTPS with a self-signed P-256 certificate**",
            self.text,
        )
        self.assertNotIn("https://<tailnet-ipv4>:4242", self.text)

    def test_base_url_section_references_adr_0013(self):
        self.assertIn("plain HTTP, not HTTPS", self.text)
        self.assertIn("adr/0013-plain-http-over-the-tailnet.md", self.text)

    def test_client_checklist_drops_pinning_delegate(self):
        self.assertNotIn("One `URLSession` with the pinning delegate", self.text)

    def test_alias_table_untouched(self):
        # §0.1 alias table and core path/field shapes must survive verbatim.
        self.assertIn("| `pid_certain` | **`agent_certain`** |", self.text)
        self.assertIn("POST /api/v1/devices/self/push", self.text)
        self.assertIn('"token": "orc1_33ba5d99_', self.text)

    def test_cert_not_after_marked_absent_not_removed(self):
        # Field shape retained (§1.6 conditional keys) but annotated as absent under ADR 0013.
        self.assertIn("cert_not_after", self.text)
        self.assertIn("**absent under ADR 0013**", self.text)


if __name__ == "__main__":
    unittest.main()
