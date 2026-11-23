# Proposal: HTTPS Support for the AI Trader Web Dashboard

## 1. Overview & Motivation

Currently, `cmd_serve` (`app/main.py`) runs `uvicorn.run(...)` over plain HTTP (`http://0.0.0.0:8000`). When accessing the dashboard over a LAN, VPN, VPS, or remote browser—especially when toggling live execution (`POST /api/execution/toggle`), flattening positions (`POST /api/execution/flatten`), or testing Telegram alerts (`POST /api/telegram/test`)—traffic is unencrypted and browsers flag the origin as **Not Secure**.

Because the dashboard already runs on **Uvicorn** (which natively supports TLS via `ssl_certfile` and `ssl_keyfile`), we can enable HTTPS cleanly with **zero extra runtime dependencies**, while also supporting reverse-proxy TLS termination (Caddy / Nginx / Cloudflare Tunnel) for public domain setups.

---

## 2. Deployment Options

| Option | Best For | Certificate Source | Browser Trust |
|---|---|---|---|
| **A. Native Uvicorn HTTPS + `mkcert`** *(Recommended for LAN / Local / VPS IP)* | Direct `serve.bat` access on `https://localhost:8000` or `https://<LAN-IP>:8000` | Locally-trusted CA generated via `mkcert` | **Green lock** (no browser warning on devices with root CA installed) |
| **B. Native Uvicorn HTTPS + Self-Signed Cert** | Quick one-command setup with zero external tools (pure Python `cryptography` / OpenSSL) | Auto-generated self-signed PEM cert (`certs/cert.pem`, `certs/key.pem`) | Encrypted TLS (requires one-time "Proceed" click in browser) |
| **C. Reverse Proxy (Caddy / Nginx)** | Public domain name (`https://trader.yourdomain.com`) | Automatic Let's Encrypt certificate via Caddy | **Publicly trusted green lock** on all devices |

---

## 3. Configuration & Code Design

### 3.1 `config.yaml` & `app/config.py`

Extend `dashboard:` with optional SSL settings (off by default so existing HTTP setups continue working unchanged):

```yaml
dashboard:
  host: 0.0.0.0
  port: 8000
  ssl_enabled: false               # set true to serve HTTPS directly from Uvicorn
  ssl_certfile: certs/cert.pem     # path to PEM certificate (or fullchain.pem)
  ssl_keyfile: certs/key.pem       # path to PEM private key
  ssl_keyfile_password: null       # optional passphrase if key is encrypted
```

Matching defaults in `app/config.py` (`DEFAULTS["dashboard"]`):

```python
"ssl_enabled": False,
"ssl_certfile": "certs/cert.pem",
"ssl_keyfile": "certs/key.pem",
"ssl_keyfile_password": None,
```

---

### 3.2 `app/main.py` (`cmd_serve` & CLI flags)

Update `cmd_serve` and `argparse` in `app/main.py`:

1. **Optional CLI overrides**:
   - `--https`: enable HTTPS on the fly without editing `config.yaml`.
   - `--ssl-certfile <path>` and `--ssl-keyfile <path>`: override certificate/key paths.
2. **Startup validation & clear startup banner**:
   - Resolve `ssl_certfile` and `ssl_keyfile` relative to the project root if relative paths are given.
   - If `ssl_enabled` is `true` and either file is missing, print a clear error message explaining how to generate them (`python -m app.main gen-cert` or `mkcert`) instead of an obscure Uvicorn traceback.
   - Print `dashboard -> https://<host>:<port> ...` when TLS is active.
   - Pass `ssl_certfile`, `ssl_keyfile`, and `ssl_keyfile_password` to `uvicorn.run(...)`.

```python
ssl_enabled = bool(cfg["dashboard"].get("ssl_enabled", False)) or bool(getattr(args, "https", False))
ssl_kwargs = {}
scheme = "http"
if ssl_enabled:
    cert_path = Path(getattr(args, "ssl_certfile", None) or cfg["dashboard"].get("ssl_certfile") or "certs/cert.pem")
    key_path = Path(getattr(args, "ssl_keyfile", None) or cfg["dashboard"].get("ssl_keyfile") or "certs/key.pem")
    if not cert_path.is_file() or not key_path.is_file():
        print(f"[error] HTTPS enabled but certificate/key not found: {cert_path}, {key_path}")
        print("        Run `python -m app.main gen-cert` or configure dashboard.ssl_certfile / ssl_keyfile")
        sys.exit(1)
    ssl_kwargs["ssl_certfile"] = str(cert_path)
    ssl_kwargs["ssl_keyfile"] = str(key_path)
    if cfg["dashboard"].get("ssl_keyfile_password"):
        ssl_kwargs["ssl_keyfile_password"] = str(cfg["dashboard"]["ssl_keyfile_password"])
    scheme = "https"

print(f"dashboard -> {scheme}://{cfg['dashboard']['host']}:{cfg['dashboard']['port']} "
      f"({len(syms)} symbols){auto_txt}")
uvicorn.run(app, host=cfg["dashboard"]["host"], port=int(cfg["dashboard"]["port"]),
            log_level="warning", **ssl_kwargs)
```

---

### 3.3 Built-in Self-Signed Certificate Generator (`python -m app.main gen-cert`)

Add a `gen-cert` CLI subcommand (`cmd_gen_cert` in `app/main.py`) so users on Windows don't need OpenSSL installed manually:

- Uses `cryptography` (or `openssl` CLI fallback) to generate a 2048-bit RSA / ECDSA P-256 key and an X.509 certificate valid for 825 days with **Subject Alternative Names (SANs)** covering:
  - `DNS:localhost`
  - `IP:127.0.0.1`
  - The machine's detected LAN IPv4 address (plus any extra `--hosts` passed on CLI, e.g. `--hosts 192.168.1.50 my-vps.local`).
- Writes `certs/cert.pem` and `certs/key.pem` (with `certs/` added to `.gitignore` so private keys are never committed).

---

### 3.4 Frontend & Security Hygiene (`app/dashboard/server.py` & `.gitignore`)

1. **Relative API URLs**: Verify `app/dashboard/static/index.html` uses relative paths (`/api/...`) rather than hardcoded `http://` URLs so all fetch calls automatically inherit `https://` with zero mixed-content errors.
2. **HSTS & Security Headers** (optional middleware in `app/dashboard/server.py` when `ssl_enabled` is true):
   - Add `X-Content-Type-Options: nosniff` and `X-Frame-Options: SAMEORIGIN`.
3. **`.gitignore`**:
   - Ignore `certs/*.pem` and `certs/*.key` so private TLS keys are never tracked in git.

---

## 4. Setup Guides

### Option A: Browser-Trusted Local / LAN HTTPS with `mkcert` (Recommended)

1. Install `mkcert` on Windows (e.g. `winget install FiloSottile.mkcert` or `choco install mkcert`).
2. Install the local root CA once:
   ```bat
   mkcert -install
   ```
3. Generate a certificate for localhost and your LAN IP inside the `ai-trader` folder:
   ```bat
   mkdir certs
   mkcert -cert-file certs\cert.pem -key-file certs\key.pem localhost 127.0.0.1 192.168.1.100
   ```
4. Enable HTTPS in `config.yaml`:
   ```yaml
   dashboard:
     ssl_enabled: true
     ssl_certfile: certs/cert.pem
     ssl_keyfile: certs/key.pem
   ```
5. Start the server (`serve.bat`) and open `https://localhost:8000` or `https://192.168.1.100:8000`.

### Option B: Zero-Dependency Self-Signed Cert

```bat
python -m app.main gen-cert --hosts 192.168.1.100
serve.bat --https
```

### Option C: Public Domain via Caddy Reverse Proxy

Keep `dashboard.host: 127.0.0.1` and `dashboard.ssl_enabled: false` in `config.yaml`, and point Caddy at `127.0.0.1:8000` for automatic Let's Encrypt HTTPS:

```caddyfile
trader.yourdomain.com {
    reverse_proxy 127.0.0.1:8000
}
```

---

## 5. Verification Plan

1. Run `python -m app.main gen-cert` and confirm `certs/cert.pem` and `certs/key.pem` are created with SANs for `localhost` and `127.0.0.1`.
2. Run `serve.bat --https` and verify startup logs `dashboard -> https://0.0.0.0:8000 ...`.
3. Request `https://127.0.0.1:8000/api/meta` and open the dashboard in a browser to confirm the UI loads cleanly over HTTPS with no mixed-content warnings.
