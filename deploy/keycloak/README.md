# The `support` realm (T-002)

`support-realm.json` is imported by compose's Keycloak on first start. It is the
whole identity configuration of cycle 1, written by hand so it can be read:

| Piece | What it does |
|---|---|
| roles `orders:read` … `escalations:review` | the coarse scopes; the realm-role mapper emits them in `scp` |
| role `customer`, role `reviewer` | composites of those scopes, and the only roles users hold. Their names appear in `scp` too, and grant nothing, because the agent reads only scopes it knows |
| attribute `customer_id` | the store customer a login is linked to. **View and edit: admin only**, so a customer cannot relink themselves |
| client scope `support-session` | the claims the agent requires: `sub`, `customer_id`, `scp`, and `aud: support-agent`. Keycloak adds `iss`, `exp`, `iat` and `jti` itself. Defined here rather than inherited from the default `basic` scope, which an import that lists its own scopes does not create |
| client `support-chat` | public. **Password grant is on for local tests only**; the customer's real login arrives with the channel (T-026) |
| client scope `order-system-audience` | adds `aud: order-system`. Optional on `support-agent` only, so the agent is the one client that can exchange a customer's session for the order system, and must ask for it by name |
| client `support-agent` | confidential, the agent's own identity, with standard token exchange on. Exchanges need `audience=order-system` **and** `scope=order-system-audience`; either alone is refused |
| client `support-approvals` | the approval workflow's own login (T-028). The customer is not there an hour later, so it logs in as itself and the order system reads whose the call is from the approval it names. Its service account holds `orders:read` and nothing else — `refunds:write` is on no token anywhere |
| client `order-system` | the audience exchanged tokens are addressed to |
| client `support-portal` | confidential, PKCE S256: the backend that logs a customer in and keeps their session server-side for the chat channel (T-026). Password grant on for local tests only |
| users `c-1042`, `c-9999`, `desk-1` | two customers, because an ownership defect takes two to see (F-016), and a reviewer with no customer. Password `local-dev-only` |

Keycloak never overwrites an existing realm on import. After changing the file:

```bash
docker compose exec postgres psql -U postgres -c "DROP DATABASE keycloak WITH (FORCE)" -c "CREATE DATABASE keycloak OWNER keycloak"
docker compose up -d --force-recreate keycloak
```
