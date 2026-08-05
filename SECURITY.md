# Security policy

Gecko is security-sensitive by design: it comprehends untrusted specs, injects
credentials at call time, and gates signing decisions.

**Reporting.** Found a vulnerability? Email ernanibmurtinho@gmail.com — please do not
open a public issue for exploitable findings. We aim to acknowledge within 72 hours.

**Scope highlights.** The anti-poisoning sanitizer and quarantine, the auth-injection
firewall (out-of-band host anchoring), the SSRF netguard, the never-sign/never-send
boundary (AST-enforced), and the control-plane invariant (no payloads, no secrets
stored). A relaxation of any detection rule goes through security review before merge.
