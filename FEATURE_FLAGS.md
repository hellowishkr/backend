# Feature Flags — Safety Controls for New Flows

Default: **All flags disabled** for production safety.

Enable explicitly in staging with environment variables. Do not enable in production unless verified in staging.

## Flags

### `enable_shared_wishlist`
**Scope:** Wishlist sharing and friend gift-giving flow  
**Env var:** `ENABLE_SHARED_WISHLIST=true`  
**Default:** `false`

**Controls:**
- `get_friend_wishlist` — view a celebrant's shared wishlist items
- `reserve_wishlist_item` — claim an item before purchase (48h hold)
- `confirm_wishlist_purchase` — mark item as purchased

**When disabled:**
- These tools return `{"status": "disabled", "message": "Wishlist sharing is not enabled yet."}`
- Agent can still call them but gets a polite disabled response
- No WhatsApp messages sent to celebrants
- No reservations created
- No purchase records created

**Rollout order:**
1. Enable in staging, test end-to-end
2. Monitor for errors in logs (no regressions expected)
3. Verify gift-giver → celebrant messages flow correctly
4. Verify reservation lifecycle (create → expire/confirm)
5. Enable in production after successful staging window (1–2 weeks)

---

### `preference_outbound_enabled`
**Scope:** Outbound preference request collection  
**Env var:** `PREFERENCE_OUTBOUND_ENABLED=true`  
**Default:** `false`

**Controls:**
- `request_recipient_preferences` — send preference request to recipient

**When disabled:**
- Tool returns `{"status": "disabled", "message": "Preference request messaging is not enabled yet."}`
- Agent can call it but no WhatsApp message is sent
- No PreferenceRequest row is created
- No preference collection flow starts

**Rollout order:**
1. Enable in staging, test preference request triggers
2. Monitor outbound WhatsApp delivery (check Meta webhook logs)
3. Verify recipients receive the preference request message
4. Test end-to-end: request → recipient reply → preference saved
5. Enable in production after successful staging window (1–2 weeks)

**Notes:**
- This flag gates the *outbound* message only. Inbound preference replies are handled separately (see `preference_interceptor_enabled`).
- Even if disabled, preferences can be provided via other flows (e.g., saved earlier).

---

### `preference_interceptor_enabled`
**Scope:** Inbound preference reply interception  
**Env var:** `PREFERENCE_INTERCEPTOR_ENABLED=true`  
**Default:** `false`

**Controls:**
- `_handle_preference_reply_if_pending` — intercept inbound messages for preference replies

**When disabled:**
- Function returns `None` immediately
- Message falls through to normal agent pipeline
- Recipient's reply is handled as a normal conversation turn
- No preference extraction/saving

**Rollout order:**
1. Enable outbound messaging (`preference_outbound_enabled`) first; verify recipients can be contacted
2. Once outbound is stable (3–5 days), enable interceptor in staging
3. Monitor for false-positives: messages that look like preferences but aren't (e.g., "I love this bot")
4. Verify correctly parsed preferences are saved (check preference records)
5. Test edge cases: expired requests, duplicate requests, fast replies
6. Enable in production after successful staging window (1–2 weeks)

**Notes:**
- Only intercepts if there's a pending `PreferenceRequest` for the phone
- Safe to enable; worst case it passes a message through if unsure
- Requires `preference_outbound_enabled` to actually create requests

---

## Environment Examples

**Development (all disabled):**
```bash
# .env (or default if omitted)
ENABLE_SHARED_WISHLIST=false
PREFERENCE_OUTBOUND_ENABLED=false
PREFERENCE_INTERCEPTOR_ENABLED=false
```

**Staging (all enabled for testing):**
```bash
ENABLE_SHARED_WISHLIST=true
PREFERENCE_OUTBOUND_ENABLED=true
PREFERENCE_INTERCEPTOR_ENABLED=true
```

**Production Phase 1 (shared wishlist):**
```bash
ENABLE_SHARED_WISHLIST=true
PREFERENCE_OUTBOUND_ENABLED=false
PREFERENCE_INTERCEPTOR_ENABLED=false
```

**Production Phase 2 (add outbound):**
```bash
ENABLE_SHARED_WISHLIST=true
PREFERENCE_OUTBOUND_ENABLED=true
PREFERENCE_INTERCEPTOR_ENABLED=false
```

**Production Phase 3 (add interceptor):**
```bash
ENABLE_SHARED_WISHLIST=true
PREFERENCE_OUTBOUND_ENABLED=true
PREFERENCE_INTERCEPTOR_ENABLED=true
```

---

## Testing

All flags have tests:
- Disabled behavior (returns safe response or no-op)
- Enabled behavior (calls the tool/function)
- Default state (all false)

Run tests:
```bash
pytest tests/test_feature_flags.py -v
```

Existing wishlist tests require explicit flag patches when testing enabled behavior (see `test_wishlist_sharing.py` for examples).

---

## Monitoring

When enabling flags in staging/production:

1. **Logs:** Check for `failed to log` messages (non-critical)
2. **Agent logs:** Look for `feature_disabled` or disabled response messages (expected if flag off)
3. **WhatsApp delivery:** Monitor Meta webhook logs for message delivery status
4. **Database:** Check `preference_requests` and `recipient_preferences` tables for new rows
5. **Errors:** Watch for `PreferenceRequest` constraint violations (e.g., duplicate requests)

---

## Disable a Flag

If issues arise, disable the flag immediately via environment:
```bash
ENABLE_SHARED_WISHLIST=false
```

No database rollback needed — the flag only gates execution, not schema.
No user-facing impact — agent falls back to other code paths gracefully.
