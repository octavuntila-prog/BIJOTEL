# BIJOTEL

> **Tamper-evident HMAC audit chain for LLM applications.**

Every AI decision. Sealed. Verified. Forever.

```bash
pip install bijotel
```

## What BIJOTEL does

BIJOTEL plugs into your LLM call pipeline via OpenTelemetry
SpanProcessors. Every call gets:

- **HMAC-SHA256 chain** — each entry links to the previous;
  any modification breaks the chain at the exact entry.
- **Content-addressable storage** — semantic dedup,
  identical prompts stored once.
- **Policy gate** — 50 jailbreak patterns + AST code safety
  checked pre-call.
- **Regression detection** — z-score + IQR drift monitoring
  on your chain data.
- **Energy & carbon accounting** — Wh and gCO₂ per call,
  per region grid intensity.

## Quick links

<div class="grid cards" markdown>

-   :material-rocket-launch: **Get going**

    ---

    Install in seconds, instrument in 3 lines.

    [:octicons-arrow-right-24: Installation](getting-started/installation.md)
    [:octicons-arrow-right-24: 5-Minute Quickstart](getting-started/quickstart.md)

-   :material-shield-check: **Verify the demo**

    ---

    Run `bijotel verify-export` against a real 200-entry chain.

    [:octicons-arrow-right-24: First Verification](getting-started/first-verification.md)

-   :material-cog: **Deep guides**

    ---

    Policy engine, multi-provider chains, energy tracking.

    [:octicons-arrow-right-24: Guides](guides/policy-engine.md)

-   :material-api: **API reference**

    ---

    CLI, REST endpoints, Python public API.

    [:octicons-arrow-right-24: REST API](api/rest.md)
    [:octicons-arrow-right-24: Python API](api/python.md)

</div>

## Production validated

- **5,889+ chain entries** across 15 days continuous operation
- **2 independent production systems** — GENA (x86_64) + ARA (aarch64)
- **Cross-architecture** verification proven (R2-D)
- **Cross-provider** chains (Anthropic + xAI via OpenAI adapter)
- **686 unit tests** + **46 production tests** across 3 rounds (0 partial/fail)
- **CI green** on Python 3.11 + 3.12

## How it compares

BIJOTEL is **complementary** to existing observability tools, not a
replacement.

| Your stack | BIJOTEL |
|---|---|
| Developer experience | Cryptographic proof of integrity |
| Trace UI, debugging | Regulator-ready audit trail |
| Prompt evaluation | Tampering detection |
| Cost dashboards | Forensic chain of custody |

Wire BIJOTEL alongside Langfuse / LangSmith / Helicone in three lines.
Same OpenTelemetry spans — observed by both, sealed only by BIJOTEL.

## License

MIT licensed. Source: [github.com/octavuntila-prog/BIJOTEL](https://github.com/octavuntila-prog/BIJOTEL).
Package: [pypi.org/project/bijotel](https://pypi.org/project/bijotel/).
Landing: [bijotel.whiteandpoint.com](https://bijotel.whiteandpoint.com/).
