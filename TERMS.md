# PatchProof AI: terms of use (DRAFT)

> **Draft for review by a qualified lawyer before external users are invited.** It is a starting list of what the operator
> will probably want to say, written to match what the software actually does. It is not legal advice and does not
> guarantee any legal effect. Bracketed items must be filled in.

**Operator:** [name, address, contact] · **Governing law and venue:** [to be decided with counsel]

1. **What the service is.** PatchProof runs a Python project's tests before and after a proposed patch in an isolated
   sandbox and reports an evidence-based verdict. A verdict of "verified for tested cases" means only that the tests that
   exist passed. It does not mean the patch is correct, secure or fit for any purpose.
2. **Your code and your rights.** You keep all rights to your code. You confirm you may use any repository you connect
   and that sending its content to the service and, in AI mode, to the model provider does not breach a licence, contract
   or confidentiality duty. The operator needs permission to process it only to provide the service ([confirm wording with counsel]).
3. **Patches.** Patches you download or open as pull requests are yours. Review every patch before merging. The service
   only ever opens **draft** pull requests, and only after you approve the exact change.
4. **Acceptable use.** No attacking the service or others, no attempts to escape the sandbox, no excessive load, no code or
   repositories you are not allowed to run, no malware, and no sharing of access tokens. The service may limit, suspend or
   remove access, and quotas apply (the free plan allows [30] verifications per month).
5. **AI output.** In AI mode a model proposes patches. It can be wrong. Its output is treated as untrusted and is
   verified by tests, but tests can be incomplete.
6. **Availability.** The service is a beta, provided as is and as available, without uptime commitments, and may change or
   stop. Back up anything you need; run records are deleted after the retention period in the [privacy notice](PRIVACY.md).
7. **No warranty, limited liability.** [Counsel to draft; the usual position is no warranties of accuracy or fitness, and a
   cap on liability to the extent the law allows.]
8. **Payments.** Billing is not implemented. No payment is taken, and no terms for fees exist yet.
9. **Changes and contact.** The operator may update these terms and will say so in the [changelog](CHANGELOG.md). Contact: [email].

*The software itself is licensed to everyone under the MIT licence in [LICENSE](LICENSE). These terms are about using an
operator's hosted instance.*
