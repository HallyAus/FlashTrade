# Cloudflare Tunnel Setup — trade.printforge.com.au

> Route traffic from trade.printforge.com.au → FlashTrade API inside your LXC.
> Since you already have Cloudflare Tunnels running for other services, this
> adds a new route to your existing tunnel.

## Option A: Add to Existing Tunnel (recommended)

You already have a tunnel running for printforge.com.au. Just add the new route.

### 1. SSH into your LXC (or wherever cloudflared runs)

```bash
ssh root@<flashtrade-lxc-ip>
```

### 2. Install cloudflared (if not on this LXC yet)

```bash
curl -L --output cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
dpkg -i cloudflared.deb
rm cloudflared.deb
```

### 3. Authenticate (one-time)

```bash
cloudflared tunnel login
```

This opens a browser link — pick the printforge.com.au zone.

### 4. If using your existing tunnel

Find your tunnel name/ID:
```bash
cloudflared tunnel list
```

Add the route by editing `~/.cloudflared/config.yml`:

```yaml
tunnel: <your-tunnel-id>
credentials-file: /root/.cloudflared/<your-tunnel-id>.json

ingress:
  # ... your existing routes ...

  # FlashTrade
  - hostname: trade.printforge.com.au
    service: http://localhost:8000

  # Catch-all (must be last)
  - service: http_status:404
```

### 5. If creating a NEW tunnel on this LXC

```bash
# Create tunnel
cloudflared tunnel create flashtrade

# Add DNS route
cloudflared tunnel route dns flashtrade trade.printforge.com.au
```

Then create `~/.cloudflared/config.yml`:

```yaml
tunnel: flashtrade
credentials-file: /root/.cloudflared/<tunnel-id>.json

ingress:
  - hostname: trade.printforge.com.au
    service: http://localhost:8000
  - service: http_status:404
```

### 6. Add the DNS record

If `cloudflared tunnel route dns` didn't do it automatically:

1. Go to [Cloudflare Dashboard](https://dash.cloudflare.com) → printforge.com.au → DNS
2. Add a CNAME record:
   - **Name**: `trade`
   - **Target**: `<tunnel-id>.cfargotunnel.com`
   - **Proxy**: ON (orange cloud)

### 7. Start/restart the tunnel

```bash
# If running as a service:
systemctl restart cloudflared

# Or run directly to test:
cloudflared tunnel run flashtrade
```

### 8. Install as a service (if new tunnel)

```bash
cloudflared service install
systemctl enable cloudflared
systemctl start cloudflared
```

### 9. Verify

```bash
# From inside the LXC — check the API is up
curl http://localhost:8000

# From anywhere — check the tunnel
curl https://trade.printforge.com.au
```

You should see:
```json
{
  "name": "FlashTrade",
  "version": "0.1.0",
  "status": "running",
  "trading_mode": "paper"
}
```

## Troubleshooting

| Problem | Fix |
|---------|-----|
| DNS not resolving | Wait 1-2 min for propagation, check CNAME exists in Cloudflare DNS |
| 502 Bad Gateway | FlashTrade not running — `docker compose ps` inside LXC |
| Connection refused | cloudflared not running — `systemctl status cloudflared` |
| Wrong tunnel | Check `cloudflared tunnel list` and config.yml tunnel ID match |

## Security Notes

- Cloudflare handles HTTPS termination — no certs to manage
- The LXC only needs to expose port 8000 internally, not to the internet
- Add Cloudflare Access later (Day 25) for proper auth
