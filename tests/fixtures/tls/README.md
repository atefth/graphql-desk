# Test-only TLS certificates

These public fixtures use a disposable RSA key and an isolated test CA. Never use
the key or trust this CA outside tests. The CA private key is not included.

`localhost.pem` covers `localhost` and `127.0.0.1`; `wrong-host.pem` covers only
`wrong.invalid`; both are valid from 2020 through 2120. `expired.pem` expired in
2021. All three use `server-key.pem` and are signed by `ca.pem`. They were generated
with OpenSSL; tests use Python's verified TLS transport without extra dependencies.
