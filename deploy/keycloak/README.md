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
| client `support-agent` | confidential, the agent's own identity, with standard token exchange on (T-002 commit C) |
| client `order-system` | the audience exchanged tokens are addressed to |
| users `c-1042`, `c-9999`, `desk-1` | two customers, because an ownership defect takes two to see (F-016), and a reviewer with no customer. Password `local-dev-only` |

Keycloak never overwrites an existing realm on import. After changing the file:

```bash
docker compose exec postgres psql -U postgres -c "DROP DATABASE keycloak WITH (FORCE)" -c "CREATE DATABASE keycloak OWNER keycloak"
docker compose up -d --force-recreate keycloak
```
