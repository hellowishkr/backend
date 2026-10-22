# Multilingual chat routing — 22 October 2026

Natural-language messages now pass through the history-aware intent model and
the main conversational agent. The model chooses tools and phrases the answer
from their results. The removed phrase router, product-link fast path, greeting
shortcut, English keyword fallback, and fixed conversational acknowledgements
no longer decide what a customer meant.

Tools retain deterministic ownership, input, feature-flag, and action checks.
`get_order_support` returns owned-order facts and the support contact before
registration, including legacy phone aliases. It does not change an order,
request a review, or notify the team. `set_chat_preferences` stores any requested
language/style and clears abandoned flow state. `record_recipient_preferences`
accepts model-extracted preference fields only for the caller's live request.

Explicit unrestricted budgets are tool arguments, so they do not depend on
English/Hinglish phrase matching. Name corrections and date confirmations use
the existing validated write tools. A missing intent-router result leaves the
complete permitted tool surface available to the main model.

The system prompt previously named only English, Hindi, and Hinglish. A live
Kannada replay exposed that restriction. The permanent prompt now supports any
language; the intent model also supplies an inferred reply-language hint.
Explicit current requests and saved language preferences take precedence.
The static prompt remains constant across turns; dates and customer facts stay
in turn context.

Tool errors continue through the model for localized explanation. Local
rate-limit, outage, and empty-response fallbacks remain available when a model
cannot produce a reply. Regex used for data validation/redaction is separate
from conversational intent recognition.

Validation:

- 889 tests passed, 1 opt-in live smoke test skipped. The full run includes
  PostgreSQL refund and payment-link concurrency checks against a disposable DB.
- Changed-file Ruff checks pass. Repository-wide mypy still reports 95 existing
  errors in 24 files; no clean repository-wide type-check claim is made.
- Ten live intent replays selected the expected tools: order requests in English,
  Hindi, Marathi, Kannada, Tamil, Spanish, and Arabic; a Hindi language request,
  a Marathi name correction, and a Spanish closure.
- Five live main-model replays selected the expected tools. Order explanations
  were inspected in Hindi, Kannada, Spanish, and Arabic after the language fix.
- Orchestration tests verify model replies are preserved, real tool writes occur,
  another buyer's order is inaccessible, pending dates can be confirmed, closures
  clear pending state, and English tool errors do not override localized replies.
- Replays used fictional inputs. No customer messages or financial actions were
  sent, and production customer records were not modified for these replays.
