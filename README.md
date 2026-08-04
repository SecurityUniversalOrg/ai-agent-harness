# ai-agent-harness

VulnHunter security-audit, automation, verification, and remediation tooling.

Start with the [architecture documentation](docs/architecture/README.md) for
system context, component and deployment views, runtime workflows, design
decisions, and the current quality/risk assessment.

For classifier-free `claude-mythos-5` execution, use the dedicated
[Mythos security and gVisor deployment profile](docs/architecture/mythos-security-profile.md).

Subsystem documentation:

- [VulnHunter audit skill](vulnhunt/README.md)
- [VulnHunter headless agent](vulnhunter-agent/README.md)
- [VulnHunter fix-verification skill](vulnhunt-fix-verify/README.md)
- [VulnHunter remediation skill](vulnhunter-fix/README.md)
