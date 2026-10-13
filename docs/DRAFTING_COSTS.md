# Compact drafting and model call costs

The default connected draft makes one model call. It does not automatically ask
for corrections after a schema/archive error or failed test build. The draft UI
has an unchecked option for a separate advisory review; checking it requests one
additional model call. Local schema, archive and test-build checks still run
without paid model calls. Test builds that skip unavailable game data are not
proof of game preparation or gameplay.

The complete initial system/user prompt is capped at 24,000 characters. It uses
the operation/schema contract, one fictional example and bounded public evidence
from the target repository. It does not include other games' catalog recipes or
a prewritten matching recipe. Full manifests and catalog definitions stay on the
server for deterministic validation. The model receives capability names when a
matching supported preparation path exists, not instructions telling it how that
game's recipe must be assembled. If evidence is insufficient, say so and expose
what is missing; do not invent support.

The limit is characters, not exact tokenizer counts or a dollar-price promise.
Output length and the configured model's rates also affect charges. Existing
records retain the exact prompts and responses for inspection. A user-triggered
retry is another billable request. An operator can explicitly enable the legacy
correction/review behavior through draft_max_calls, draft_test_rounds and
draft_model_review settings; these are not the compact defaults.

Empty definition inputs means no server packaging inputs are configured. It does
not mean the game needs no ROM or other owned game data. A runtime-only recipe
must expose what the player must provide on first launch. Preparing a new port
can require a new fixed tool adapter; a language model cannot safely invent an
executable operation that the runner does not implement.

The longer-term design is bounded evidence retrieval on demand (README sections,
release layout, specific source files) and provider-reported token/call accounting
visible before approval. Discovery and executable packaging remain separate:
the model investigates; Portkeeper validates and executes supported operations.
Neither model confidence nor advisory model review establishes correctness.
